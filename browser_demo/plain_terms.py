# -*- coding: utf-8 -*-
"""Plain-language labels for the non-AI simulator screen (app/MASKIT_Desktop.html).

Only on-screen wording changes; values, ids and calculations stay the same. Each old text must be found
exactly once, so a later edit of the HTML cannot be silently skipped.
usage: python plain_terms.py ../app/MASKIT_Desktop.html
"""
import sys

PAIRS = [
    ('<h1>컨테이너 정렬 보정 시뮬레이터 · 센서 이력 추정 · 성공사례 누적 v3.5.2 (PC 저장형)</h1>',
     '<h1>MAS-kit 컨테이너 정렬 시뮬레이터 v3.6</h1>'),
    ('코너 ① 확대', '모서리 ① 확대'),
    ('하드웨어 오차 가정 (원본 3종)', '장비 상태'),
    ('기준 가정 · 편향 0.5μH / 공극 1mm', '보통 현장 · 오차 큼 (센서 치우침 0.5μH, 틈 변화 1mm)'),
    ('목표 가정 · 편향 0.25μH / 공극 0.3mm', '목표 성능 센서 (치우침 0.25μH, 틈 변화 0.3mm)'),
    ('FEM 기준 가정 · 편향 0μH', '이상적 조건 · 해석값 그대로 (오차 없음)'),
    ('위치 추정 방식 (기본: 이력 추정 / 언제든 원본 복귀)', '구멍 위치 계산 방식'),
    ('센서 이동 이력으로 정적 편향 추정·제거 — 권장 비교안', '이동 기록 활용 · 센서 치우침 자동 제거 (추천)'),
    ('기존 격자 추정기 — 원본 롤백', '한 번 잰 값만 사용 (옛 방식)'),
    ('기존 격자 + 위치별 X/Y LM — 이전 실험안', '옛 방식 + 미세 보정 (이전 실험)'),
    ('컨테이너 코너 공차', '컨테이너 모서리 제작 오차'),
    ('원본 가정 (하드웨어 가정의 코너 공차)', '장비 상태에 맞춤 (기본)'),
    ('ISO 668 규격 기반 (S·P +3/−5 mm, 대각선 차 ≤13 mm)', '국제 규격(ISO 668) 허용 범위'),
    ('<label for="capture">결합 판정</label>', '<label for="capture">잠금 성공 기준</label>'),
    ('원본: 네 코너 모두 ±5 mm', '네 모서리 모두 ±5 mm 안 (기본)'),
    ('고정 트위스트록: 길이 ±10 · 폭 ±3.8 mm (구멍 124.5×63.5)', '고정형 잠금쇠: 길이 ±10 · 폭 ±3.8 mm'),
    ('플로팅 트위스트록: ±10 mm (리치스태커 스프레더 표준 사양)', '움직이는 잠금쇠: ±10 mm (리치스태커 보통 사양)'),
    ('<label for="radius">초기 오차 반경</label>', '<label for="radius">처음 어긋난 거리 (최대)</label>'),
    ('시나리오·초기 오차 반경', '장비 상태·처음 어긋난 거리'),
    ('40 mm (외삽 위험)', '40 mm (해석 범위 밖이라 부정확)'),
    ('1회 시드 (재현용)', '상황 번호 (같은 번호 = 같은 상황)'),
    ('매 실행마다 새 무작위 시드 (끄면 입력 시드 재현)', '실행할 때마다 새 상황 (끄면 위 번호의 상황을 다시 재현)'),
    ('▶ 1회 작업 실행 (랜덤 오차)', '▶ 작업 1건 실행'),
    ('>새 랜덤 시드<', '>새 상황 번호<'),
    ('마지막 1회 실행 시드: —', '마지막 실행 상황 번호: —'),
    ("'마지막 1회 실행 시드: '", "'마지막 실행 상황 번호: '"),
    ('최종/현재 최악축', '가장 많이 어긋난 모서리'),
    ('③ 랜덤 오차 100~1,000회 성공률 시험', '③ 여러 번 돌려 성공률 보기 (100~1,000회)'),
    ('<label for="batchseed">시험 시드</label>', '<label for="batchseed">시작 상황 번호</label>'),
    ('누적 사례 활용 (하강 판단)', '과거 기록으로 내릴지 판단'),
    ('켬 — 비슷한 과거 사례 결합률 30% 미만이면 내리지 않고 보류', '켬 — 비슷한 과거 상황의 잠금 성공이 30% 미만이면 내리지 않음'),
    ('켬 — 40% 미만이면 보류 (더 보수적)', '켬 — 40% 미만이면 내리지 않음 (더 조심)'),
    ('끔 — 원본: 6회 보정 후 항상 하강', '끔 — 6번 보정 후 항상 내림 (기본)'),
    ('Wilson 95% 구간', '성공률 오차 범위 (95%)'),
    ('외삽 포함 사례', '해석 범위 밖을 지난 경우'),
    ('최종 오차 중앙값 / 95백분위', '최종 어긋남: 보통 / 나쁜 5%'),
    ('보류 → 재접근 후 성공 / 신호수 수동', '멈춤 → 다시 접근해 성공 / 사람이 직접'),
    ('④ 누적 성공·실패 사례 보관함', '④ 실행 기록 보관함'),
    ('⑤ 메인 서버 연동 · 4대 1세트 키트', '⑤ 메인 서버와 키트 (모서리 4개 = 1세트)'),
    ('편향(고장 미감지)', '값이 치우침 (고장인 줄 모름)'),
    ('키트 코너 고장', '키트 모서리 고장'),
    ('코너2 헤드 2개 고장(감지)', '모서리2 코일 2개 고장 (키트가 알아챔)'),
    ('코너2 교정 틀어짐(미감지)', '모서리2 보정값 틀어짐 (키트가 모름)'),
    ('4코너 종합 방식', '네 모서리 값 합치는 방법'),
    ('신뢰도 가중 + 튀는 코너 제외', '믿을 만한 값에 비중 + 튀는 모서리 제외 (추천)'),
    ('>4코너 동일 가중<', '>네 모서리 같은 비중<'),
    ('고정 기준 (대각선 1·3)', '대각선 모서리 1·3만 사용'),
    ('v3.5.4 그대로 (인터록 없음)', '옛 방식 v3.5.4 (확인 없이 내림)'),
    ('\\n기계 센서: ', '\\n기존 센서: '),
    ('\\n코너 신뢰도: ', '\\n모서리별 신뢰도: '),
    (' · 코너 가중치: ', ' · 모서리별 비중: '),
    ("`\\n보정 이동: X ${fmt(d.correction.x_mm,2)} / Y ${fmt(d.correction.y_mm,2)} mm / θ ${fmt(d.correction.theta_deg,4)}°",
     "`\\n보정 이동: 가로 ${fmt(d.correction.x_mm,2)} / 세로 ${fmt(d.correction.y_mm,2)} mm / 회전 ${fmt(d.correction.theta_deg,4)}°"),
    ('° (기계 ${Math.round(100*(d.w_machine||0))}% · 키트', '° (기존 센서 ${Math.round(100*(d.w_machine||0))}% · 키트'),
    ("${d.reason?' ('+d.reason+')':''}",
     "${d.reason?' ('+({disagree:'기존 센서와 키트 값이 어긋남',no_reliable_data:'믿을 만한 측정값 없음',sensors_disagree:'두 센서가 계속 어긋남',verify_failed:'최종 확인에서 허용 범위를 넘음',verified:'최종 확인 통과'}[d.reason]||d.reason)+')':''}"),
    ('코너별 현재 상대 오차', '모서리별 지금 어긋난 정도'),
    ('회 보정 · 최종 최악축 ', '회 보정 · 가장 큰 어긋남 '),
]


def apply(text):
    for old, new in PAIRS:
        n = text.count(old)
        if n == 0 and new in text:   # already applied (script can be re-run)
            continue
        if n != 1:
            raise SystemExit('expected 1 match, found %d: %r' % (n, old))
        text = text.replace(old, new)
    return text


if __name__ == '__main__':
    path = sys.argv[1]
    src = open(path, encoding='utf-8', newline='').read()
    out = apply(src)          # check everything before touching the file
    open(path, 'w', encoding='utf-8', newline='').write(out)
    print('replaced', len(PAIRS))
