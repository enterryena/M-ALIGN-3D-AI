# -*- coding: utf-8 -*-
"""Render tools/compare_results.json as Markdown tables into docs/3단계_메인서버_가상현장.md
between the <!-- RESULTS --> markers."""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOC = ROOT / 'docs' / '3단계_메인서버_가상현장.md'
RES = ROOT / 'app' / 'tools' / 'compare_results.json'
STRAT = {'v354': 'v3.5.4 그대로 (인터록 없음)', 'equal': '4코너 동일 가중', 'diagonal': '고정 기준 (대각선 1·3)',
         'weighted': '신뢰도 가중 + 튀는 코너 제외'}
FAULT = {'none': '고장 없음', 'head_dead': '헤드 2개 고장(감지)', 'drift': '교정 틀어짐(미감지)'}
MACH = {'normal': '정상', 'unstable': '불안정', 'none': '정보 없음', 'biased': '편향(고장 미감지)'}
HW = {'target': '목표 하드웨어', 'base': '기준 가정'}


def pct(k, n):
    return '%.1f%%' % (100.0 * k / n)


def main():
    data = json.loads(RES.read_text(encoding='utf-8'))
    n = data['n']
    lines = ['', '각 칸: 성공 / **잘못된 하강** / 안전 정지 (%d회씩, 같은 난수로 짝지은 비교)' % n, '']
    rows = [r for r in data['rows'] if r['set'] == 'A']
    for hw in ('target', 'base'):
        lines += ['### A. 4코너 종합 방식 — %s, 기존 센서 정보 없음' % HW[hw], '',
                  '| 코너 고장 | ' + ' | '.join(STRAT[s] for s in ('v354', 'equal', 'diagonal', 'weighted')) + ' |',
                  '|---|' + '---|' * 4]
        for fault, fc in (('none', None), ('head_dead', 1), ('drift', 1), ('head_dead', 2), ('drift', 2)):
            cells = []
            for s in ('v354', 'equal', 'diagonal', 'weighted'):
                r = next(x for x in rows if x['hardware'] == hw and x['strategy'] == s and x['fault'] == fault
                         and x['fault_corner'] == fc)
                cells.append('%s / **%s** / %s' % (pct(r['success'], n), pct(r['false_lower'], n), pct(r['held'], n)))
            label = FAULT[fault] + ('' if fc is None else ' · 코너%d%s' % (fc, ' (기준 코너)' if fc == 1 else ''))
            lines.append('| %s | %s |' % (label, ' | '.join(cells)))
        lines.append('')
    rows = [r for r in data['rows'] if r['set'] == 'B']
    lines += ['### B. 기존 센서와 우선순위 — 신뢰도 가중 방식, 코너 고장 없음', '',
              '| 기존 센서 | 목표 하드웨어 | 기준 가정 | 평균 회차 (목표/기준) | 작업당 재측정 (목표/기준) |', '|---|---|---|---|---|']
    for m in ('normal', 'unstable', 'none', 'biased'):
        t = next(x for x in rows if x['hardware'] == 'target' and x['machine'] == m)
        b = next(x for x in rows if x['hardware'] == 'base' and x['machine'] == m)
        lines.append('| %s | %s / **%s** / %s | %s / **%s** / %s | %.1f / %.1f | %.2f / %.2f |' % (
            MACH[m], pct(t['success'], n), pct(t['false_lower'], n), pct(t['held'], n),
            pct(b['success'], n), pct(b['false_lower'], n), pct(b['held'], n),
            t['avg_rounds'], b['avg_rounds'], t['resense_per_job'], b['resense_per_job']))
    text = DOC.read_text(encoding='utf-8')
    a, b = text.index('<!-- RESULTS -->'), text.index('<!-- /RESULTS -->')
    DOC.write_text(text[:a] + '<!-- RESULTS -->' + '\n'.join(lines) + '\n' + text[b:], encoding='utf-8')
    print('updated', DOC)


if __name__ == '__main__':
    main()
