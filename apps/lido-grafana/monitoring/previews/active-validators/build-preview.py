"""Build two Grafana TestData dashboards; never contacts live metrics or keys."""
import argparse
import copy
import csv
import io
import json
import math
from pathlib import Path
import random

BASE = Path(__file__).resolve().parents[2] / 'dashboards/lido-validator-fleet.json'
DS = {'type': 'testdata', 'uid': 'lido-mock'}
START = 1790294400000  # 2026-09-25 00:00 UTC
END = START + 86400000
RNG = random.Random(26)


def target(names, rows, ref='A'):
    output = io.StringIO()
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(names)
    writer.writerows(rows)
    return {'refId': ref, 'datasource': DS, 'scenarioId': 'csv_content', 'csvContent': output.getvalue()}


def mock(panel, names, rows):
    panel['datasource'] = DS
    panel['targets'] = [target(names, rows)]
    panel.pop('transformations', None)
    return panel


def stat(panel, values):
    panel['datasource'] = DS
    panel['targets'] = [target(['Time', name], [[END, value]], chr(65+i)) for i, (name, value) in enumerate(values)]
    panel.pop('transformations', None)
    return panel


def position(panel, x, y, w, h):
    panel['gridPos'] = dict(x=x, y=y, w=w, h=h)
    return panel


def card(template, ident, title, value, x, y, w, h, balance=False):
    p = copy.deepcopy(template)
    p.update(id=ident, title=title, description='MOCK DATA — CSV inventory and beacon-state lookup. This preview is not live monitoring.')
    p['fieldConfig']['overrides'] = []
    p['fieldConfig']['defaults'].update(unit='suffix: ETH' if balance else 'none', decimals=2 if balance else 0,
        color={'mode':'fixed', 'fixedColor':'blue' if balance else 'green'})
    p['options']['textMode'] = 'value'
    stat(p, [(title, value)])
    return position(p, x, y, w, h)


def chart(panel, names, series):
    times = [START + i * 60000 for i in range(1441)]
    panel['datasource'] = DS
    panel['targets'] = [target(['Time', name], [[t, round(values[i], 5)] for i, t in enumerate(times)], chr(65+j)) for j, (name, values) in enumerate(zip(names, series))]
    panel.pop('transformations', None)
    return panel


def table(panel, headers, rows):
    mock(panel, headers, rows)
    panel['fieldConfig']['defaults']['unit'] = 'none'
    return panel


def build(output):
    base = json.loads(BASE.read_text())
    panels = {p['id']:p for p in base['panels']}
    rates = [[max(0, center + RNG.gauss(0, .13) + .06*math.sin(i/21)) for i in range(1441)] for center in (2.109, 1.758, 1.406)]
    proposals = [[1 if 719 <= i <= 723 else 0 for i in range(1441)], [0]*1441, [0]*1441]
    cpu = [.045 + .008*math.sin(i/31) + RNG.uniform(-.004,.004) for i in range(1441)]
    ram = [.615 + .006*math.sin(i/130) + RNG.uniform(-.002,.002) for i in range(1441)]

    fleet = copy.deepcopy(base)
    fleet.update(id=None, uid='mock-active-fleet', title='MOCK DATA · Fleet overview · Active validators', refresh='', version=1)
    fleet['time'] = {'from':'2026-09-25T00:00:00.000Z','to':'2026-09-26T00:00:00.000Z'}
    fleet['panels'] = [copy.deepcopy(panels[n]) for n in (1,3,4,2,7,8,40,38,39)]
    fp = {p['id']:p for p in fleet['panels']}
    position(stat(fp[3], [('Loaded',50)]), 0,1,4,2)
    fleet['panels'][2:2] = [card(fp[3],41,'Active validators',45,4,1,3,2), card(fp[3],42,'Active balance',1440.38,7,1,4,2,True)]
    position(stat(fp[4], [('Attestations',10125),('Proposals',1)]),11,1,7,2)
    position(stat(fp[2], [('Reporting now',1),('Scrape history',1)]),18,1,6,2)
    chart(fp[7], ['validator-1','validator-2','validator-3'],rates)
    chart(fp[8], ['validator-1','validator-2','validator-3'],proposals)
    accuracy=[]
    for j in range(6):
        accuracy.append([.999 - (.003 if j%3==0 else .001)*max(0,math.sin(i/41+j)) - (.011*max(0,1-abs(i-430)/35) if j%3==0 else 0) for i in range(1441)])
    chart(fp[40], ['main Source','main Target','main Head','fallback Source','fallback Target','fallback Head'], accuracy)
    fp[40]['title']='Accuracy · 1h'
    table(fp[38], ['Instance','VC metrics','Loaded keys','Active','Active ETH','Attestations','Proposals','VC RAM','CPU','Host RAM','Good links'],
        [['VC 1',1,20,18,576.18,4050,1,255*2**20,.045,.615,2],
         ['VC 2',1,15,15,480.12,3375,0,294*2**20,.033,.621,2],
         ['VC 3',1,15,12,384.08,2700,0,331*2**20,.03,.627,2]])
    fp[38]['fieldConfig']['overrides'] += [{'matcher':{'id':'byName','options':'Active ETH'},'properties':[{'id':'unit','value':'none'},{'id':'decimals','value':2}]}]
    table(fp[39], ['Instance','EC metrics','CL metrics','EC peers','CL peers','EC RAM','CL RAM','CPU','Host RAM','Head accuracy'],
        [['main',1,1,50,44,9.8*2**30,5.2*2**30,.185,.48,.998],
         ['fallback',1,1,48,43,'',4.2*2**30,.11,.39,.996]])

    vc = copy.deepcopy(base)
    vc.update(id=None, uid='mock-active-vc1', title='MOCK DATA · VC 1 overview · Active validators', refresh='', version=1)
    vc['time'] = fleet['time']
    variable=vc['templating']['list'][0]
    variable['current']={'text':['VC 1'],'value':['validator-1']}
    for option in variable['options']: option['selected']=option['value']=='validator-1'
    vc['panels']=[copy.deepcopy(panels[n]) for n in (9,10,12,14,15,17,16,18,37)]
    vp={p['id']:p for p in vc['panels']}
    vp[9].pop('repeat',None)
    vp[9]['title']='Validator client · VC 1'
    position(vp[9],0,0,24,1)
    position(stat(vp[10],[('Metrics',1),('Loaded keys',20)]),0,1,3,3)
    vc['panels'][2:2]=[card(fp[3],43,'Active validators',18,3,1,3,3),card(fp[3],44,'Active balance',576.18,6,1,4,3,True)]
    position(stat(vp[12],[('Attestations',4050),('Proposals',1)]),10,1,4,3)
    position(stat(vp[14],[('VC RAM',255*2**20)]),14,1,3,3)
    position(stat(vp[15],[('CPU',.045),('RAM',.615)]),17,1,4,3)
    position(stat(vp[17],[('good',2),('viable',0),('bad',0)]),21,1,3,3)
    for i in (10,12,14,15,17): vp[i]['options']['text']['valueSize']=25
    position(chart(vp[16],['Attestations / 45 s','Proposals / 5 min'],[rates[0],proposals[0]]),0,4,12,7)
    position(chart(vp[18],['CPU','RAM'],[cpu,ram]),12,4,7,7)
    position(chart(vp[37],['good','viable','bad'],[[2]*1441,[0]*1441,[0]*1441]),19,4,5,7)

    output.mkdir(parents=True,exist_ok=True)
    for name,dash in [('fleet',fleet),('vc1',vc)]:
        dash['timezone']='utc'
        dash['tags']=['mock-data','layout-preview']
        dash['description']='Mock layout preview. 50 loaded keys / 45 active. Active balances are synthetic consensus balances. These TestData queries do not run the production CSV collector.'
        (output/f'{name}.json').write_text(json.dumps(dash))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    build(parser.parse_args().output)
