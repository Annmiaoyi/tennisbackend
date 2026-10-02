"""
converter.py — 把数据库里的标注导出成训练管线可直接消费的文件，并计算一致性。

- export_annotation_json: 标准 annotation.json（TRAINING_SPEC 第 9 节格式，label 取解析后的最终值）
- build_dataset: 复用窗口逻辑，生成 38 维 dataset.csv（同样用解析后 label）
- resolve_session_annotations: 按冲击时刻聚类，取「人类多数优先、否则未审阅启发式」的最终 label
- compute_consistency: 多人标注一致性（成对一致率 + Cohen's Kappa）
"""
import csv
import json
import math
import os
from collections import Counter

from .db import RAW_DIR

# 同一击球在两端时间轴上的容差（秒）：人类标注 / 启发式预标注 / 双人匹配均用此值
MATCH_TOL = 0.3


def load_raw_payload(conn, session_id):
    """取一场会话的原始包（含 samples 波形）。**这是本模块唯一的原始数据入口。**

    三级回落，顺序不能换：
      1. L0 原始层按 raw_id 取 —— 2026-09-29 起这是权威来源，内容寻址、只追加，
         识别算法换版也能拿它重算；
      2. 会话表上的 raw_path —— 迁移期旧数据没有 raw_id，靠它兜底；
      3. `var/raw/files/raw_<sid>.json` 这个历史命名约定 —— 只为兼容更早的散落文件。

    ⚠️ 为什么必须收敛成一处：`api.get_samples`（画波形）与 `build_dataset`
    （导出训练集）原先各写了一套解析。前者改用 L0 之后，后者没跟上，
    仍然只找第 3 级路径 —— 于是出现「波形画得出来、点导出却 404 说没有样本」
    这种自相矛盾的现象，而且两端都不报错，只能靠人肉对日志才发现。
    """
    from .. import rawstore

    row = conn.execute(
        'SELECT raw_id, raw_path FROM sessions WHERE id=?', (session_id,)
    ).fetchone()
    if row is None:
        return None

    # 1) L0 权威源。显式指定 shape='raw_package' —— 只有该形态带 samples；
    #    若同一会话也被 App 以 match_session 形态上传过，不指定会取到没有波形的那份。
    if row['raw_id']:
        raw = rawstore.payload_of(session_id, shape=rawstore.SHAPE_RAW_PACKAGE)
        if raw is not None:
            return raw

    # 2) 会话表里的路径缓存
    raw_path = row['raw_path']
    if raw_path and os.path.exists(raw_path):
        try:
            with open(raw_path, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass

    # 3) 历史命名约定（var/raw/files/raw_<sid>.json）
    legacy = os.path.join(RAW_DIR, 'raw_%s.json' % session_id)
    if os.path.exists(legacy):
        try:
            with open(legacy, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None


def feat_stats(x):
    n = len(x)
    m = sum(x) / n
    var = sum((v - m) ** 2 for v in x) / n
    sd = math.sqrt(var) if var > 0 else 0.0
    skew = (sum((v - m) ** 3 for v in x) / n) / (sd ** 3) if sd > 0 else 0.0
    kurt = (sum((v - m) ** 4 for v in x) / n) / (sd ** 4) - 3 if sd > 0 else 0.0
    return [m, sd, skew, kurt, min(x), max(x)]


def window_features(samples):
    """samples: list[dict] with keys t,ax,ay,az,rx,ry,rz（与 raw_*.json 一致）"""
    ax = [s["ax"] for s in samples]
    ay = [s["ay"] for s in samples]
    az = [s["az"] for s in samples]
    rx = [s["rx"] for s in samples]
    ry = [s["ry"] for s in samples]
    rz = [s["rz"] for s in samples]
    feats = []
    for sig in (ax, ay, az, rx, ry, rz):
        feats += feat_stats(sig)
    amag = [math.sqrt(s["ax"] ** 2 + s["ay"] ** 2 + s["az"] ** 2) for s in samples]
    rmag = [math.sqrt(s["rx"] ** 2 + s["ry"] ** 2 + s["rz"] ** 2) for s in samples]
    feats.append(sum(amag) / len(amag))
    feats.append(sum(rmag) / len(rmag))
    return feats


def majority(labels):
    return Counter(labels).most_common(1)[0][0]


def resolve_session_annotations(conn, session_id):
    """返回该会话解析后的标注列表：每簇一个最终 label。
    规则：
      - 簇内若有人类标注(confirmed) → 取人类多数投票 label（source='human'）
      - 否则若簇内有未审阅的启发式(proposed) → 用其 label（source='auto'，预标注默认采纳）
      - 否则（如仅 rejected）→ 跳过
    这是「你只纠错」模式的核心：未被人工更正的启发式直接成为训练标签。
    """
    rows = conn.execute(
        "SELECT impact_time, label, annotator, source, status FROM annotations "
        "WHERE session_id=? ORDER BY impact_time",
        (session_id,),
    ).fetchall()
    if not rows:
        return []
    clusters = []
    for r in rows:
        t = r["impact_time"]
        placed = False
        for cl in clusters:
            if abs(cl["center"] - t) <= MATCH_TOL:
                cl["items"].append(r)
                placed = True
                break
        if not placed:
            clusters.append({"center": t, "items": [r]})

    resolved = []
    for cl in clusters:
        humans = [it for it in cl["items"]
                  if it["annotator"] != "heuristic" and it["status"] == "confirmed"]
        heur = [it for it in cl["items"]
                if it["annotator"] == "heuristic" and it["source"] == "auto" and it["status"] == "proposed"]
        if humans:
            label = majority([it["label"] for it in humans])
            source = "human"
        elif heur:
            label = heur[0]["label"]
            source = "auto"
        else:
            continue
        resolved.append({
            "impact_time": round(cl["center"], 4),
            "label": label,
            "source": source,
            "n_human": len(humans),
            "n_heuristic": len(heur),
        })
    return resolved


def export_annotation_json(conn, session_id=None):
    """返回 {sessionId: [{impactTime, label, source}]}，label 为解析后的最终值。"""
    if session_id:
        resolved = resolve_session_annotations(conn, session_id)
        return {session_id: [
            {"impactTime": r["impact_time"], "label": r["label"], "source": r["source"]}
            for r in resolved
        ]}
    out = {}
    sids = [r["session_id"] for r in conn.execute(
        "SELECT DISTINCT session_id FROM annotations").fetchall()]
    for sid in sids:
        resolved = resolve_session_annotations(conn, sid)
        if resolved:
            out[sid] = [
                {"impactTime": r["impact_time"], "label": r["label"], "source": r["source"]}
                for r in resolved
            ]
    return out


def build_dataset(conn, out_path):
    """对所有已标注会话，按解析后的 impact 提 [center-0.05, center+0.10] 窗口 → 38 维 dataset.csv。"""
    sids = [r["session_id"] for r in conn.execute(
        "SELECT DISTINCT session_id FROM annotations").fetchall()]
    header = [f"f{i}" for i in range(38)] + ["label"]
    count = 0
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for sid in sids:
            resolved = resolve_session_annotations(conn, sid)
            if not resolved:
                continue
            raw = load_raw_payload(conn, sid)
            if raw is None:
                continue
            samples = raw.get('samples', [])
            for r in resolved:
                it = r["impact_time"]
                win = [s for s in samples if (it - 0.05) <= s["t"] <= (it + 0.10)]
                if len(win) < 4:
                    continue
                w.writerow(window_features(win) + [r["label"]])
                count += 1
    return count


def cohen_kappa(pairs, categories):
    """pairs: list[(label_a, label_b)]；categories: 涉及的类别集合。返回 Kappa 或 None（无样本）。"""
    n = len(pairs)
    if n == 0:
        return None
    agree = sum(1 for a, b in pairs if a == b)
    po = agree / n
    ca = Counter(a for a, _ in pairs)
    cb = Counter(b for _, b in pairs)
    pe = sum((ca[c] / n) * (cb[c] / n) for c in categories)
    if pe >= 1.0:
        return 1.0
    return (po - pe) / (1 - pe)


def compute_consistency(conn, session_id=None):
    """多人标注一致性：
      - 人类标注：annotator != 'heuristic' 且 status='confirmed'
      - 启发式统计：预标注的 proposed/confirmed/rejected 数量（难例挖掘信号）
      - 成对：在共同会话上按冲击时刻聚类配对，算一致率与 Cohen's Kappa
    """
    where = "WHERE session_id=?" if session_id else ""
    args = (session_id,) if session_id else ()
    rows = conn.execute(
        f"SELECT session_id, impact_time, label, annotator, source, status "
        f"FROM annotations {where} ORDER BY session_id, impact_time",
        args,
    ).fetchall()

    # 人类标注按 (标注者, 会话) 分组
    human = {}
    for r in rows:
        if r["annotator"] == "heuristic" or r["status"] != "confirmed":
            continue
        human.setdefault(r["annotator"], {}).setdefault(r["session_id"], []).append(
            (r["impact_time"], r["label"]))

    # 启发式统计（按 会话 维度）
    heur_by_sid = {}
    for r in rows:
        if r["annotator"] != "heuristic":
            continue
        s = heur_by_sid.setdefault(r["session_id"], {"proposed": 0, "confirmed": 0, "rejected": 0})
        s[r["status"]] = s.get(r["status"], 0) + 1

    # 标注者汇总
    annotators = []
    for name, bysid in human.items():
        total = sum(len(v) for v in bysid.values())
        accepted = sum(heur_by_sid.get(sid, {}).get("proposed", 0) for sid in bysid)
        corrected = sum(heur_by_sid.get(sid, {}).get("rejected", 0) for sid in bysid)
        annotators.append({
            "name": name,
            "human_count": total,
            "sessions": sorted(bysid.keys()),
            "heuristic_accepted": accepted,
            "heuristic_corrected": corrected,
        })

    # 启发式跨会话汇总
    heur_summary = {"proposed": 0, "confirmed": 0, "rejected": 0}
    for s in heur_by_sid.values():
        for k in ("proposed", "confirmed", "rejected"):
            heur_summary[k] += s.get(k, 0)

    # 成对一致性
    names = sorted(human.keys())
    pairs = []
    all_pairs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            common = sorted(set(human[a].keys()) & set(human[b].keys()))
            paired = []
            for sid in common:
                merged = ([(t, l, "a") for t, l in human[a][sid]]
                          + [(t, l, "b") for t, l in human[b][sid]])
                clusters = []
                for t, l, who in merged:
                    placed = False
                    for cl in clusters:
                        if abs(cl["center"] - t) <= MATCH_TOL:
                            cl["items"].append((l, who))
                            placed = True
                            break
                    if not placed:
                        clusters.append({"center": t, "items": [(l, who)]})
                for cl in clusters:
                    la = [l for l, w in cl["items"] if w == "a"]
                    lb = [l for l, w in cl["items"] if w == "b"]
                    if la and lb:
                        paired.append((majority(la), majority(lb)))
            if paired:
                cats = sorted({x for p in paired for x in p})
                kappa = cohen_kappa(paired, cats)
                agree = sum(1 for x, y in paired if x == y)
                pairs.append({
                    "a": a, "b": b,
                    "matched": len(paired),
                    "agree": agree,
                    "agreement": round(agree / len(paired), 4),
                    "kappa": round(kappa, 4) if kappa is not None else None,
                })
                all_pairs.extend(paired)

    overall_kappa = None
    if all_pairs:
        cats = sorted({x for p in all_pairs for x in p})
        overall_kappa = round(cohen_kappa(all_pairs, cats), 4)

    return {
        "session_id": session_id,
        "annotators": annotators,
        "heuristic_summary": heur_summary,
        "pairs": pairs,
        "overall_kappa": overall_kappa,
        "kappa_guide": "κ<0 差 / 0 随机一致 / 0.01-0.20 极小 / 0.21-0.40 一般 / "
                       "0.41-0.60 中等 / 0.61-0.80 强 / 0.81-1 几乎完全一致",
    }
