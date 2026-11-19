"""Which base-assumption error limits success? Improve one factor at a time to its target value (1,500 jobs each)."""
import sys, json, numpy as np
import pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import maskit_engine as E, maskit_link
from multiprocessing import Pool
def init():
    global M; M = maskit_link._load_model()
def one(a):
    over, s = a; P = E.settings('base', 'history', **over); return E.run_once(M, P, E.Rng(s))['ok']
if __name__ == '__main__':
    cfgs = [('기준 가정 그대로', {}), ('센서 모델 편향 0.5→0.25μH', {'modelBias': .25}), ('공극 편차 1→0.3mm', {'gapSd': .3}), ('기울기 0.2→0.05°', {'tiltSd': .05}),
            ('컨테이너 코너 공차 3→0.7mm', {'tolC': .7}), ('이동 오차 0.5→0.2mm', {'act': .2}), ('목표 가정 전부', {'modelBias': .25, 'gapSd': .3, 'tiltSd': .05, 'tolC': .7, 'act': .2, 'noise': .2})]
    with Pool(2, initializer=init) as p:
        for name, o in cfgs:
            r = p.map(one, [(o, s) for s in range(70001, 71501)], chunksize=25); print(name, round(np.mean(r), 3), flush=True)
