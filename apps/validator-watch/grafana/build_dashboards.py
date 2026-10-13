"""Generate provisioned SQLite dashboards; queries return bounded aggregates."""
import json
from pathlib import Path
DS={'type':'frser-sqlite-datasource','uid':'validator-watch'}
RANGE='bucket >= CAST($__from / 3600000 AS INTEGER)*3600 AND bucket <= $__to/1000'
EVENT_RANGE='time >= $__from/1000 AND time <= $__to/1000'
CLIENT='client IN (${client:sqlstring})'
KEY='pubkey IN (${validator:sqlstring})'


def panel(ident,title,kind,x,y,w,h,sql,description='',unit='none'):
    p={'id':ident,'title':title,'type':kind,'gridPos':dict(x=x,y=y,w=w,h=h),'datasource':DS,
       'description':description,'targets':[{'refId':'A','datasource':DS,'rawQueryText':sql,'queryText':sql,
          'queryType':'table','timeColumns':['time']}],
       'fieldConfig':{'defaults':{'unit':unit,'decimals':0,'noValue':'No checked data','color':{'mode':'palette-classic'}},'overrides':[]},
       'options':{}}
    if kind=='stat':p['options']={'reduceOptions':{'calcs':['lastNotNull'],'fields':'','values':False},'textMode':'value','graphMode':'none','colorMode':'value','text':{'valueSize':28}}
    if kind=='table':
        p['options']={'showHeader':True,'cellHeight':'sm','footer':{'show':False}}
        p['fieldConfig']['defaults']['custom']={'minWidth':55}
        p['fieldConfig']['defaults']['noValue']='—'
    if kind=='timeseries':
        p['fieldConfig']['defaults']['custom']={'lineWidth':1,'fillOpacity':8,'showPoints':'never','axisSoftMin':0}
        p['options']={'legend':{'displayMode':'list','placement':'bottom'},'tooltip':{'mode':'multi'}}
    return p


def variables(detail=False):
    v=[{'name':'client','label':'Client','type':'query','datasource':DS,'query':'SELECT client FROM summary UNION SELECT client FROM events ORDER BY client',
        'refresh':1,'multi':True,'includeAll':True,'current':{'text':'All','value':'$__all'},'options':[]}]
    if detail:v.append({'name':'validator','label':'Validator','type':'query','datasource':DS,
       'query':f"SELECT substr(pubkey,1,12)||'…'||substr(pubkey,-6) AS __text, pubkey AS __value FROM (SELECT client,pubkey FROM summary UNION SELECT client,pubkey FROM events) WHERE {CLIENT} GROUP BY pubkey ORDER BY pubkey",
       'refresh':1,'multi':True,'includeAll':True,'current':{'text':'All','value':'$__all'},'options':[]})
    return v


def build(detail=False):
    where=RANGE+' AND '+CLIENT+(' AND '+KEY if detail else '')
    events=EVENT_RANGE+' AND '+CLIENT+(' AND '+KEY if detail else '')
    name='Validator detail' if detail else 'Fleet incidents'
    dash={'uid':'validator-watch-detail' if detail else 'validator-watch','title':'Validator Watch · '+name,
          'schemaVersion':39,'version':1,'editable':False,'tags':['validator-watch'], 'timezone':'utc',
          'time':{'from':'now-24h','to':'now'},'refresh':'1m','templating':{'list':variables(detail)},
          'links':[{'title':'Fleet incidents','type':'link','url':'/d/validator-watch','includeVars':True,'keepTime':True},
                   {'title':'Validator detail','type':'link','url':'/d/validator-watch-detail','includeVars':True,'keepTime':True}], 'panels':[]}
    p=dash['panels']
    cards=[('Collector',"SELECT CASE WHEN value='MOCK DATA' THEN value WHEN CAST((SELECT value FROM meta WHERE key='heartbeat') AS REAL)<strftime('%s','now')-600 THEN 'Stale' ELSE value END FROM meta WHERE key='collector_state'",3),
       ('Missed blocks',f"SELECT count(*) FROM events WHERE {events} AND kind='proposal' AND status='missed'",3),
       ('Affected',f'SELECT sum(affected) FROM summary WHERE {where}',5),
       ('Issue points',f'SELECT sum(source_miss+target_miss+head_miss) FROM summary WHERE {where}',4),
       ('Checked',f'SELECT sum(expected) FROM summary WHERE {where}',5),
       ('Coverage',f"SELECT 100.0*sum(status='checked')/count(*) FROM epochs WHERE {EVENT_RANGE}",4)]
    x=0
    for i,(title,sql,w) in enumerate(cards,1):
        p.append(panel(i,title,'stat',x,0,4,3,sql,'Affected = attestations missing at least one flag. Issue points = total missing source/target/head flags. Checked = observed attestation duties. Finalized observations only. Summary counts include whole hourly buckets. Coverage describes attempted epochs, not time before startup.', 'percent' if i==6 else 'none'));x+=4
    p.append(panel(7,'Component issues · hourly','timeseries',0,3,14,6,f'''WITH RECURSIVE hours(t) AS (SELECT CAST($__from/3600000 AS INTEGER)*3600 UNION ALL SELECT t+3600 FROM hours WHERE t+3600 <= $__to/1000 LIMIT 9000), totals AS (SELECT bucket,sum(source_miss) AS Source,sum(target_miss) AS Target,sum(head_miss) AS Head FROM summary WHERE {where} GROUP BY bucket) SELECT t AS time,Source,Target,Head FROM hours LEFT JOIN totals ON totals.bucket=hours.t ORDER BY t'''))
    p.append(panel(8,'Proposal outcomes · finalized','table',14,3,10,6,f"SELECT time,client AS Client,slot AS Slot,status AS Outcome FROM events WHERE {events} AND kind='proposal' ORDER BY time DESC LIMIT 50",'Scheduled slot time, not network submission time. Misses require complete canonical evidence.'))
    p.append(panel(9,'Clients · observed duty issues' if not detail else 'Validators · ranked by component issues','table',0,9,24,6,
       f'''SELECT client AS Client,{"pubkey AS Pubkey,substr(pubkey,1,12)||'…'||substr(pubkey,-6) AS Validator," if detail else ''}
       sum(expected) AS Checked,sum(affected) AS Affected,sum(source_miss) AS Source,sum(target_miss) AS Target,sum(head_miss) AS Head,
       sum(source_miss+target_miss+head_miss) AS Points,round(100.0*sum(affected)/nullif(sum(expected),0),3) AS "Issue %",
       sum(sync_missed) AS "Sync missed",sum(sync_unknown) AS "Sync unknown"
       FROM summary WHERE {where} GROUP BY client{',pubkey' if detail else ''} ORDER BY Points DESC LIMIT 200''',
       'Each failed source/target/head flag adds one point. All-three failures add three. Sync counts committee positions, including duplicate assignments. Whole hourly buckets; at most 200 rows.'))
    p.append(panel(10,'Attestation exceptions · good attestations omitted','table',0,15,24,8,
       f'''SELECT time,client AS Client,substr(pubkey,1,12)||'…'||substr(pubkey,-6) AS Validator,epoch AS Epoch,
       CASE source WHEN 1 THEN '✓' ELSE 'Miss' END AS Source,CASE target WHEN 1 THEN '✓' ELSE 'Miss' END AS Target,
       CASE head WHEN 1 THEN '✓' ELSE 'Miss' END AS Head,score AS Points,delay AS "Delay slots",pubkey AS Pubkey
       FROM events WHERE {events} AND kind='attestation' ORDER BY time DESC LIMIT 200''',
       'Participation flags, not monetary rewards. No observed inclusion is different from proven failure to submit. Most recent 200 exceptions in the selected range.'))
    p.append(panel(11,'Collection coverage · unknown is not a validator failure','table',0,23,14,6,
       f"""SELECT time,epoch AS Epoch,status AS Coverage,reason AS Reason,unknown_keys AS "Unregistered keys",CASE WHEN status='checked' OR coalesce((SELECT proposals_checked FROM backlog WHERE backlog.epoch=epochs.epoch),0)=1 THEN 'checked' ELSE 'unknown' END AS Proposals FROM epochs WHERE {EVENT_RANGE} ORDER BY epoch DESC LIMIT 100"""))
    p.append(panel(12,'Discord delivery','table',14,23,10,6,
       "SELECT created AS time,CASE WHEN instr(id,':')>0 THEN substr(id,1,instr(id,':')-1) ELSE 'demo' END AS Alert,state AS State,attempts AS Attempts,last_status AS \"HTTP status\" FROM outbox ORDER BY created DESC LIMIT 30",
       'Alerts are optional. Pending retries survive restarts. Failed deliveries need operator attention. Duplicate delivery is possible if the process crashes after Discord accepts a message.'))
    p.append(panel(13,'Recovery queue · recent epochs take priority','table',0,29,24,6,
       """SELECT e.time,b.epoch AS Epoch,
       CASE WHEN b.ownership IS NULL THEN 'Confirmation needed' ELSE 'Captured' END AS Inventory,
       CASE WHEN b.proposals_checked=1 THEN 'checked' ELSE 'unknown' END AS Proposals,
       b.attempts AS Attempts,CASE WHEN b.next_try>0 THEN b.next_try ELSE NULL END AS "Retry after",
       e.reason AS Reason,(SELECT count(*) FROM backlog) AS "Total queued"
       FROM backlog b JOIN epochs e ON e.epoch=b.epoch ORDER BY b.epoch LIMIT 100""",
       'Oldest 100 unresolved epochs, independent of the selected time range. Retry times are earliest eligibility, not appointments; live work and the historical cooldown take priority. Inventory confirmation is required when no historical observation exists. Proposal outcomes may be available while attestations remain unknown.'))
    p[-1]['targets'][0]['timeColumns']=['time','Retry after']
    for t in (8,9,10,11,12):
        next(a for a in p if a['id']==t)['options']['sortBy']=[]
    def override(p, name, properties):
        p['fieldConfig']['overrides'].append({'matcher':{'id':'byName','options':name},'properties':properties})
    colors={'Source':'green','Target':'yellow','Head':'blue'}
    for field,color in colors.items():
        override(p[6],field,[{'id':'color','value':{'mode':'fixed','fixedColor':color}}])
    p[0]['options']['reduceOptions']['fields']='/.*/'
    p[0]['options']['text']['valueSize']=22
    p[0]['fieldConfig']['defaults']['unit']='string'
    p[0]['fieldConfig']['defaults']['mappings']=[{'type':'value','options':{k:{'text':k,'color':color} for k,color in [('healthy','green'),('waiting','yellow'),('unknown','red'),('Stale','red'),('MOCK DATA','blue')]}}]

    p[5]['fieldConfig']['defaults']['decimals']=1
    for table in (p[7],p[8],p[9]):
        override(table,'Client',[{'id':'unit','value':'string'}])
    for table in (p[7],p[9],p[10],p[11],p[12]):
        override(table,'time',[{'id':'custom.width','value':170}])
    override(p[9],'Validator',[{'id':'custom.width','value':190}])
    override(p[9],'Delay slots',[{'id':'noValue','value':'No inclusion'}])
    override(p[8],'Issue %',[{'id':'decimals','value':3},{'id':'unit','value':'percent'}])
    if detail:
        override(p[8],'Pubkey',[{'id':'unit','value':'string'},{'id':'custom.hidden','value':True}])
        override(p[8],'Validator',[{'id':'custom.width','value':190},{'id':'links','value':[{'title':'Inspect validator','url':'/d/validator-watch-detail?var-client=${__data.fields.Client}&var-validator=${__data.fields.Pubkey:raw}&from=$__from&to=$__to'}]}])
        for field,width in [('Client',85),('Checked',80),('Affected',80),('Source',65),('Target',65),('Head',65),('Points',65),('Issue %',85),('Sync missed',100),('Sync unknown',110)]:
            override(p[8],field,[{'id':'custom.width','value':width}])
    override(p[9],'Pubkey',[{'id':'unit','value':'string'},{'id':'custom.hidden','value':True}])
    override(p[9],'Validator',[{'id':'links','value':[{'title':'Inspect validator','url':'/d/validator-watch-detail?var-client=${__data.fields.Client}&var-validator=${__data.fields.Pubkey:raw}&from=$__from&to=$__to'}]}])
    override(p[8],'Client',[{'id':'links','value':[{'title':'Inspect client','url':'/d/validator-watch-detail?var-client=${__value.raw}&from=$__from&to=$__to'}]}])
    for table in (p[7],p[9],p[10],p[11],p[12]):
        for field in ('Outcome','Source','Target','Head','Coverage','State','Proposals'):
            if table is p[12] and field != 'Proposals':continue
            if field == 'Proposals' and table not in (p[10],p[12]):continue
            override(table,field,[{'id':'mappings','value':[{'type':'value','options':{
                **{k:{'text':k,'color':'green'} for k in ('included','✓','checked','sent')},
                **{k:{'text':k,'color':'red'} for k in ('missed','Miss','failed')},
                **{k:{'text':k,'color':'yellow'} for k in ('unknown','pending')}
            }}]},{'id':'custom.cellOptions','value':{'type':'color-text'}}])
    p[1]['fieldConfig']['defaults'].update(color={'mode':'thresholds'},thresholds={'mode':'absolute','steps':[{'color':'green','value':None},{'color':'red','value':1}]})
    return dash


if __name__=='__main__':
    for detail in (False,True):
        (Path(__file__).parent/'dashboards'/('detail.json' if detail else 'fleet.json')).write_text(json.dumps(build(detail),indent=2)+'\n')
