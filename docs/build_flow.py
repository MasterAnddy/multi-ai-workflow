"""フロー図（docs/flow.html）を生成する。flow.png はこのHTMLをブラウザで描画して書き出したもの。"""
import html
import pathlib

OUT = pathlib.Path(__file__).resolve().parent

TITLE = "複数AIの分業と再発防止フロー"
W, H = 1760, 1032
X0, COL = 208, 252
LANE_TOP, LANE_H = 156, 134
NW, NH = 224, 96
RIGHT = X0 + COL * 6


def cx(g):
    return X0 + COL * g + COL // 2


def cy(lane):
    return LANE_TOP + LANE_H * lane + LANE_H // 2


LANES = [
    ("私", "方針と判断"),
    ("Gemini", "別視点のレビュー"),
    ("Claude Code", "進行管理と受入判定"),
    ("Codex・Antigravity", "実装・調査・操作"),
    ("機械チェック", "プログラムで判定"),
]
PHASES = [("1", "方針", 0, 1), ("2", "作業", 1, 1), ("3", "レビュー", 2, 1), ("4", "外部操作", 3, 2), ("5", "改善", 5, 1)]

# key: (grid column, lane, kind, title, sub lines)
NODES = {
    "goal": (0, 0, "human", "目的と分担の決定", ["役割分担、切り替えの基準、", "レビューの順序を決める"]),
    "plan": (0, 2, "ai", "作業計画と指示書", ["要件を整理し、", "受入基準つきの指示書を作る"]),
    "precheck": (0, 4, "gate", "指示書の事前チェック", ["重複した指示や", "他の作業との競合を防ぐ"]),
    "lock": (1, 4, "gate", "対象ごとの排他ロック", ["同じ対象のロックは", "一つの担当のみ保持できる"]),
    "make": (1, 3, "ai", "専用チャットで作業", ["対象ごとに実装、調査、", "ブラウザ操作を進める"]),
    "first": (2, 3, "ai", "一次レビュー", ["作成者とは別の担当が確認", "利用枠に応じClaudeと分担"]),
    "final": (2, 2, "ai", "最終レビュー", ["指摘の妥当性を確かめ、", "反映する修正を選ぶ"]),
    "alt": (2, 1, "ai", "別視点のレビュー", ["表現と読みやすさを確認し、", "修正案を提示する"]),
    "fact": (2, 0, "human", "前提と仕様の確認", ["未確認事項は", "AIに推測で補わせない"]),
    "apply": (3, 3, "ai", "修正を新しい版に反映", ["版番号を付け、", "元の版はそのまま残す"]),
    "gate": (3, 4, "gate", "実行前チェック", ["成果物とレビュー記録を", "ハッシュで照合する"]),
    "approve": (4, 0, "human", "外部操作の承認", ["対象と操作は", "私が承認した範囲に限る"]),
    "run": (4, 3, "ai", "実行と記録", ["実施済みか確認して進め、", "結果を読み戻して台帳に記録"]),
    "done": (4, 4, "gate", "完了とロック解放", ["保持者を照合し、", "完了記録を残して解放する"]),
    "rule": (5, 0, "human", "方針の見直し", ["役割分担と", "レビューの順序を再設定する"]),
    "cause": (5, 2, "ai", "原因の整理", ["既存のルールで", "防げなかった原因を記録する"]),
    "addcheck": (5, 3, "ai", "チェックの追加", ["チェック処理とテストを", "コードで実装する"]),
}


def box(key):
    g, lane, *_ = NODES[key]
    x, y = cx(g), cy(lane)
    return x - NW // 2, y - NH // 2, x + NW // 2, y + NH // 2


def node_svg(key):
    g, lane, kind, title, subs = NODES[key]
    x, y = cx(g), cy(lane)
    left, top, right, bottom = box(key)
    if kind == "gate":
        c = 13
        pts = [(left + c, top), (right - c, top), (right, top + c), (right, bottom - c), (right - c, bottom),
               (left + c, bottom), (left, bottom - c), (left, top + c)]
        shape = f'<polygon points="{" ".join(f"{a},{b}" for a, b in pts)}"/>'
    else:
        shape = f'<rect x="{left}" y="{top}" width="{NW}" height="{NH}" rx="9"/>'
    title_y = y - 16 if len(subs) == 2 else y - 7
    sub_y = [y + 10, y + 32] if len(subs) == 2 else [y + 19]
    lines = [f'<text class="n-title" x="{x}" y="{title_y}">{html.escape(title)}</text>']
    lines += [f'<text class="n-sub" x="{x}" y="{sy}">{html.escape(s)}</text>' for s, sy in zip(subs, sub_y)]
    return f'<g class="node {kind}">{shape}{"".join(lines)}</g>'


def route(points, radius=9):
    d = [f"M{points[0][0]} {points[0][1]}"]
    for i in range(1, len(points) - 1):
        (ax, ay), (bx, by), (nx, ny) = points[i - 1], points[i], points[i + 1]
        la = max(abs(bx - ax), abs(by - ay))
        lb = max(abs(nx - bx), abs(ny - by))
        r = min(radius, la / 2, lb / 2)
        sx = bx - (bx - ax) / la * r
        sy = by - (by - ay) / la * r
        ex = bx + (nx - bx) / lb * r
        ey = by + (ny - by) / lb * r
        d.append(f"L{sx:g} {sy:g}Q{bx} {by} {ex:g} {ey:g}")
    d.append(f"L{points[-1][0]} {points[-1][1]}")
    return "".join(d)


def arrow(points, dashed=False, both=False):
    cls = "flow dashed" if dashed else "flow"
    start = ' marker-start="url(#tip)"' if both else ""
    return f'<path class="{cls}" d="{route(points)}"{start} marker-end="url(#tip)"/>'


L = lambda key: box(key)[0]
T = lambda key: box(key)[1]
R = lambda key: box(key)[2]
B = lambda key: box(key)[3]
CX = lambda key: cx(NODES[key][0])
CY = lambda key: cy(NODES[key][1])
LOOP_Y = 846
SKIP_Y = 692

ARROWS = [
    arrow([(CX("goal"), B("goal")), (CX("plan"), T("plan"))]),
    arrow([(CX("plan"), B("plan")), (CX("precheck"), T("precheck"))]),
    arrow([(R("precheck"), CY("precheck")), (L("lock"), CY("lock"))]),
    arrow([(CX("lock"), T("lock")), (CX("make"), B("make"))]),
    arrow([(R("make"), CY("make")), (L("first"), CY("first"))]),
    arrow([(CX("first"), T("first")), (CX("final"), B("final"))]),
    arrow([(CX("final"), T("final")), (CX("alt"), B("alt"))], both=True),
    arrow([(R("final"), CY("final") + 18), (CX("apply"), CY("final") + 18), (CX("apply"), T("apply"))]),
    arrow([(R("final"), CY("final") - 18), (972, CY("final") - 18), (972, CY("fact")), (R("fact"), CY("fact"))],
          dashed=True, both=True),
    # レビュー対象外は実行前チェックへ直行する
    arrow([(CX("make") + 60, B("make")), (CX("make") + 60, SKIP_Y), (956, SKIP_Y), (956, CY("gate")),
           (L("gate"), CY("gate"))], dashed=True),
    arrow([(CX("apply") + 30, B("apply")), (CX("gate") + 30, T("gate"))]),
    arrow([(CX("gate") - 30, T("gate")), (CX("apply") - 30, B("apply"))], dashed=True),
    arrow([(R("gate"), CY("gate")), (1210, CY("gate")), (1210, CY("run")), (L("run"), CY("run"))]),
    arrow([(CX("approve"), B("approve")), (CX("run"), T("run"))]),
    arrow([(CX("run"), B("run")), (CX("done"), T("done"))]),
    arrow([(R("done"), CY("done")), (1462, CY("done")), (1462, CY("cause")), (L("cause"), CY("cause"))],
          dashed=True),
    arrow([(CX("cause"), T("cause")), (CX("rule"), B("rule"))], dashed=True, both=True),
    arrow([(CX("cause"), B("cause")), (CX("addcheck"), T("addcheck"))]),
    arrow([(CX("addcheck"), B("addcheck")), (CX("addcheck"), LOOP_Y), (CX("precheck"), LOOP_Y),
           (CX("precheck"), B("precheck"))]),
]

LABELS = [
    (984, CY("alt") + 5, "start", "必要な未確認事項は私へ"),
    (CX("first"), SKIP_Y + 24, "middle", "レビュー対象外のとき"),
    (CX("gate") - 40, T("gate") - 14, "end", "不合格"),
    (1454, CY("cause") + 5, "end", "問題が起きたら"),
]

POINTS = [
    ("作成とレビューを分ける", "レビュー対象の文章は、作成者とは別のAIが確認する"),
    ("取り消せない操作の前で止める", "実施済みか確認し、合格した版だけを承認範囲内で扱う"),
    ("失敗を仕組みに変える", "再発防止策を記録し、必要に応じてチェックを追加する"),
    ("限られた利用枠を重要工程に充てる", "一次レビューをCodexに任せ、最終レビューの枠を残す"),
]

parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-labelledby="flow-title flow-desc" xmlns="http://www.w3.org/2000/svg">']
parts.append(f'<title id="flow-title">{TITLE}</title>')
parts.append('<desc id="flow-desc">私が目的、役割分担、切り替えの基準、レビューの順序を決める。Claude Codeが作業計画と指示書を作り、'
             '機械チェックと排他ロックを通してCodexとAntigravityが作業する。一次レビュー、最終レビュー、別視点のレビューを経て修正を新しい版に反映し、'
             '実行前チェックに合格した版を、私が承認した範囲で実行して記録する。問題が起きたら原因を整理し、必要に応じてチェックを追加する。</desc>')
parts.append('<defs><marker id="tip" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
             'orient="auto-start-reverse"><path class="tip" d="M0 0L10 5L0 10z"/></marker></defs>')
parts.append(f'<rect class="paper" width="{W}" height="{H}"/>')
parts.append(f'<text class="h1" x="40" y="58">{TITLE}</text>')
parts.append('<text class="lead" x="40" y="92">判断・作業・チェックを分けて進める、個人の標準運用フロー</text>')

# legend
parts.append('<g class="legend">'
             '<g class="node human"><rect x="1158" y="46" width="28" height="18" rx="4"/></g><text x="1194" y="61">私の判断</text>'
             '<g class="node ai"><rect x="1282" y="46" width="28" height="18" rx="4"/></g><text x="1318" y="61">AIの作業</text>'
             '<g class="node gate"><polygon points="1411,46 1429,46 1434,51 1434,59 1429,64 1411,64 1406,59 1406,51"/></g>'
             '<text x="1442" y="61">機械チェック</text>'
             '<path class="flow dashed" d="M1560 55H1588"/><text x="1596" y="61">必要なときだけ</text></g>')

# phase header
for num, name, g, span in PHASES:
    x, w, y, h = X0 + COL * g + 4, COL * span - 8, 112, 38
    parts.append(f'<polygon class="phase" points="{x},{y} {x + w - 14},{y} {x + w},{y + h // 2} {x + w - 14},{y + h} {x},{y + h}"/>')
    parts.append(f'<text class="phase-t" x="{x + w / 2 - 6:g}" y="{y + 25}"><tspan class="phase-n">{num}</tspan>　{name}</text>')

# lanes
for i, (name, role) in enumerate(LANES):
    y = LANE_TOP + LANE_H * i
    if i % 2 == 1:
        parts.append(f'<rect class="lane" x="40" y="{y}" width="{RIGHT - 40}" height="{LANE_H}"/>')
    parts.append(f'<text class="l-name" x="52" y="{y + 62}">{html.escape(name)}</text>')
    parts.append(f'<text class="l-role" x="52" y="{y + 88}">{html.escape(role)}</text>')
parts.append(f'<path class="rule" d="M40 {LANE_TOP}H{RIGHT}M40 {LANE_TOP + LANE_H * 5}H{RIGHT}"/>')

parts += ARROWS
parts += [node_svg(key) for key in NODES]
for x, y, anchor, text in LABELS:
    parts.append(f'<text class="edge" x="{x}" y="{y}" text-anchor="{anchor}">{html.escape(text)}</text>')
mid = (CX("precheck") + CX("addcheck")) // 2
parts.append(f'<rect class="paper" x="{mid - 170}" y="{LOOP_Y - 13}" width="340" height="26"/>')
parts.append(f'<text class="loop" x="{mid}" y="{LOOP_Y + 6}">追加したチェックで次回から止める</text>')

# footer
parts.append('<text class="f-head" x="40" y="900">運用で重視していること</text>')
step = (RIGHT - 40) / 4
for i, (head, body) in enumerate(POINTS):
    x = 40 + step * i
    parts.append(f'<text class="f-title" x="{x:g}" y="934">{html.escape(head)}</text>')
    parts.append(f'<text class="f-body" x="{x:g}" y="962">{html.escape(body)}</text>')
parts.append("</svg>")
SVG = "".join(parts)

DARK = ("--bg:#11181d; --ink:#e7eff3; --muted:#a3b4bd; --line:#4a5a63; --lane:#18232a; --card:#1c282f; "
        "--human:#58aed8; --human-ink:#06202d; --gate:#eea640; --gate-bg:#2b2113; --band:#26343c; --arrow:#9fb1ba; color-scheme:dark;")
STYLE = """
/* 工程表：横軸が工程、縦軸が担当。人の判断は塗り、機械チェックは面取りの関門で区別する */
:root {
  --bg:#fafbfb; --ink:#16242c; --muted:#4f616b; --line:#a9b7bf; --lane:#eff3f5; --card:#ffffff;
  --human:#0e4a6b; --human-ink:#ffffff; --gate:#a65800; --gate-bg:#fff2df; --band:#dde6ea; --arrow:#43545c;
  --font:"BIZ UDPGothic","Yu Gothic UI","Meiryo",sans-serif;
}
@media screen and (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { """ + DARK + """ } }
@media screen { :root[data-theme="dark"] { """ + DARK + """ } }
body { background:var(--bg); color:var(--ink); font-family:var(--font); }
.sheet { padding-block:16px; padding-left:max(16px, env(safe-area-inset-left, 0px)); padding-right:max(16px, env(safe-area-inset-right, 0px)); }
.scroll { overflow-x:auto; }
.scroll svg { display:block; width:100%; min-width:1120px; max-width:1760px; height:auto; margin-inline:auto; font-family:var(--font); }
.paper { fill:var(--bg); }
.h1 { fill:var(--ink); font-size:36px; font-weight:700; }
.lead { fill:var(--muted); font-size:19px; }
.legend text { fill:var(--muted); font-size:15px; }
.phase { fill:var(--band); }
.phase-t { fill:var(--ink); font-size:19px; font-weight:700; text-anchor:middle; }
.phase-n { fill:var(--human); }
.lane { fill:var(--lane); }
.rule { stroke:var(--line); stroke-width:1; fill:none; }
.l-name { fill:var(--ink); font-size:21px; font-weight:700; }
.l-role { fill:var(--muted); font-size:16px; }
.node rect, .node polygon { stroke-width:1.6; }
.node.ai rect { fill:var(--card); stroke:var(--line); }
.node.human rect { fill:var(--human); stroke:var(--human); }
.node.gate polygon { fill:var(--gate-bg); stroke:var(--gate); stroke-width:2; }
.n-title { fill:var(--ink); font-size:18px; font-weight:700; text-anchor:middle; }
.n-sub { fill:var(--muted); font-size:16px; text-anchor:middle; }
.node.human .n-title, .node.human .n-sub { fill:var(--human-ink); }
.flow { fill:none; stroke:var(--arrow); stroke-width:1.8; }
.flow.dashed { stroke-dasharray:6 5; }
.tip { fill:var(--arrow); }
.edge { fill:var(--muted); font-size:15px; }
.loop { fill:var(--gate); font-size:16.5px; font-weight:700; text-anchor:middle; }
.f-head { fill:var(--muted); font-size:16px; font-weight:700; }
.f-title { fill:var(--ink); font-size:20px; font-weight:700; }
.f-body { fill:var(--muted); font-size:16px; }
.f-note { fill:var(--muted); font-size:15px; }
@page { size:A4 landscape; margin:10mm; }
@media print {
  :root { --muted:#34444c; --bg:#ffffff; }
  html, body { margin:0; background:#fff; }
  .sheet { padding:0 !important; }
  .scroll { overflow:visible; }
  .scroll svg { width:277mm; min-width:0; max-width:none; -webkit-print-color-adjust:exact; print-color-adjust:exact; }
}
"""

PAGE = (
    '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    f"<title>{TITLE}</title>"
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=BIZ+UDPGothic:wght@400;700&display=swap">'
    f"<style>{STYLE}</style></head>"
    f'<body><main class="sheet"><div class="scroll">{SVG}</div></main></body></html>\n'
)
(OUT / "flow.html").write_text(PAGE, encoding="utf-8", newline="\n")
print("built", OUT / "flow.html")
