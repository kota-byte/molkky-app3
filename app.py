import os
import streamlit as st
import pandas as pd
from datetime import datetime
from sqlalchemy import text
from streamlit_drawable_canvas import st_canvas
import streamlit.components.v1 as components

st.set_page_config(page_title="モルック戦術支援アプリ", layout="centered")

db_conn = st.connection("sql", type="sql")

_SWIPE_HISTORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend_components", "swipe_history")
_swipe_history_component = components.declare_component("swipe_history", path=_SWIPE_HISTORY_DIR)

_FULLSCREEN_COURT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend_components", "fullscreen_court")
_fullscreen_court_component = components.declare_component("fullscreen_court", path=_FULLSCREEN_COURT_DIR)

# --- 1. データベース準備 ---
@st.cache_resource
def init_db():
    with db_conn.session as s:
        s.execute(text('''CREATE TABLE IF NOT EXISTS throw_logs
                     (id SERIAL PRIMARY KEY,
                      dist REAL, target_no INTEGER, is_success INTEGER,
                      timestamp TEXT)'''))
        s.execute(text('''CREATE TABLE IF NOT EXISTS players
                     (id SERIAL PRIMARY KEY,
                      name TEXT UNIQUE)'''))
        obstacle_cols = ['n', 'ne', 'e', 'se', 's', 'sw', 'w', 'nw']
        for col in obstacle_cols:
            s.execute(text(f"ALTER TABLE throw_logs ADD COLUMN IF NOT EXISTS {col} INTEGER DEFAULT 0"))
        s.execute(text("ALTER TABLE throw_logs ADD COLUMN IF NOT EXISTS player TEXT"))

        # 導入前の既存データ（プレイヤー未設定）は kota のデータとして移行
        s.execute(text("INSERT INTO players (name) VALUES ('kota') ON CONFLICT (name) DO NOTHING"))
        s.execute(text("UPDATE throw_logs SET player = 'kota' WHERE player IS NULL"))
        s.commit()

init_db()

import math
import random

def get_obstacle_flags(target_num, skittle_coords, threshold=0.225, skittle_diameter=0.059):
    """狙ったスキットルの周囲の障害物フラグを返す
    threshold: スキットル側面同士の隙間の閾値（デフォルト22.5cm）"""
    if skittle_coords[target_num] is None:
        return {}

    tx, ty = skittle_coords[target_num]
    flags = {d: 0 for d in ['n','ne','e','se','s','sw','w','nw']}
    center_threshold = threshold + skittle_diameter  # 側面間隙間→中心間距離に変換

    for num, coord in skittle_coords.items():
        if num == target_num or coord is None:
            continue
        dx = coord[0] - tx  # 右が正
        dy = coord[1] - ty  # 奥が正
        dist = math.sqrt(dx**2 + dy**2)

        if dist <= center_threshold:
            angle = math.degrees(math.atan2(dx, dy))
            if   -22.5 <= angle <  22.5: flags['n']  = 1
            elif  22.5 <= angle <  67.5: flags['ne'] = 1
            elif  67.5 <= angle < 112.5: flags['e']  = 1
            elif 112.5 <= angle < 157.5: flags['se'] = 1
            elif angle >= 157.5 or angle < -157.5: flags['s'] = 1
            elif -157.5 <= angle < -112.5: flags['sw'] = 1
            elif -112.5 <= angle <  -67.5: flags['w']  = 1
            elif  -67.5 <= angle <  -22.5: flags['nw'] = 1

    return flags

def get_fall_offset():
    """倒れたスキットルの移動量を返す（dx, dy）。
    距離は1m付近にピーク（裾の長い分布）、方向は奥(投擲方向)を中心に左右にブレる"""
    distance = min(5.0, random.lognormvariate(0.0, 0.6))
    angle_deg = random.gauss(0, 30)
    angle_rad = math.radians(angle_deg)
    dx = distance * math.sin(angle_rad)
    dy = distance * math.cos(angle_rad)
    return dx, dy

def choose_unintended_hit(target_num, coords_dict, max_dist=1.0):
    """得点あり失敗時に誤って倒れるスキットルを、距離に応じた重み付きで選ぶ
    max_dist: この距離以内のスキットルのみ候補とする"""
    target_coord = coords_dict.get(target_num)
    if target_coord is None:
        return None

    candidates = []
    weights = []
    for n, c in coords_dict.items():
        if n == target_num or c is None:
            continue
        dist = math.sqrt((c[0]-target_coord[0])**2 + (c[1]-target_coord[1])**2)
        if dist <= max_dist:
            candidates.append(n)
            weights.append(1.0 / (dist + 0.1))  # 近いほど重みが大きい

    if not candidates:
        return None
    return random.choices(candidates, weights=weights, k=1)[0]

def move_skittle_trace(coords_dict, num):
    """num番のスキットルを倒れた位置に移動（トレース表示用）"""
    if coords_dict.get(num) is None:
        return
    x, y = coords_dict[num]
    dx, dy = get_fall_offset()
    new_x = max(-2.0, min(2.0, x + dx))
    new_y = max(0.1, min(10.0, y + dy))
    coords_dict[num] = (new_x, new_y)

BASE_RATE = {
    3.0: 0.70, 3.5: 0.65, 4.0: 0.60, 4.5: 0.55,
    5.0: 0.50, 5.5: 0.45, 6.0: 0.40, 6.5: 0.35,
    7.0: 0.30, 7.5: 0.25, 8.0: 0.20, 8.5: 0.15,
    9.0: 0.12, 9.5: 0.10, 10.0: 0.08
}
K = 4  # データ件数による信頼度の調整値（大きいほどベース値を重視）

def get_success_rate(dist, obstacle_flags, df):
    """ベース値（距離別）にデータ実測値を重み付けして混ぜる"""
    base = BASE_RATE.get(round(dist * 2) / 2, 0.5)

    dirs = ['n','ne','e','se','s','sw','w','nw']
    mask = df['dist'] == dist
    for d in dirs:
        mask &= (df[d] == obstacle_flags.get(d, 0))
    matched = df[mask]
    n = len(matched)

    if n == 0:
        return base

    actual_rate = (matched['is_success'] == 1).mean()
    weight = n / (n + K)
    return base * (1 - weight) + actual_rate * weight


def trace_one_simulation(target_num, my_score, my_miss, opp_score, opp_miss, skittle_coords, df):
    """1回分のシミュレーションを実行し、各ターンの座標変化を記録して返す"""
    OPP_RATE = {
        3.0:0.70, 3.5:0.65, 4.0:0.60, 4.5:0.55,
        5.0:0.50, 5.5:0.45, 6.0:0.40, 6.5:0.35,
        7.0:0.30, 7.5:0.25, 8.0:0.20, 8.5:0.15,
        9.0:0.12, 9.5:0.10, 10.0:0.08
    }

    log = []
    sim_coords = {n: c for n, c in skittle_coords.items()}
    ms, mm, os_, om = my_score, my_miss, opp_score, opp_miss

    log.append(("初期配置", dict(sim_coords), ms, os_))

    my_rate = get_success_rate(
        round(sim_coords[target_num][1] / 0.5) * 0.5,
        get_obstacle_flags(target_num, sim_coords), df
    )
    result = random.random()
    if result < my_rate:
        ms += target_num
        move_skittle_trace(sim_coords, target_num)
        log.append((f"自分: {target_num}番成功", dict(sim_coords), ms, os_))
    elif result < my_rate + 0.15:
        mm = 0
        hit = choose_unintended_hit(target_num, sim_coords)
        if hit is not None:
            ms += hit
            move_skittle_trace(sim_coords, hit)
            log.append((f"自分: 得点あり失敗(意図せず{hit}番)", dict(sim_coords), ms, os_))
    else:
        mm += 1
        log.append((f"自分: 完全ミス({mm}回目)", dict(sim_coords), ms, os_))
        if mm >= 3:
            log.append(("→ 自分3連続ミスで相手の勝利", dict(sim_coords), ms, os_))
            return log

    my_turn = False
    for turn_i in range(10):  # 最初の10ターンだけ記録
        active_now = [n for n, c in sim_coords.items() if c is not None]
        if not active_now:
            break
        if my_turn:
            best = min(active_now, key=lambda n: abs(ms + n - 50) if ms + n <= 50 else abs(ms + n - 25 - 50))
            rate2 = get_success_rate(
                round(sim_coords[best][1] / 0.5) * 0.5,
                get_obstacle_flags(best, sim_coords), df
            )
            r2 = random.random()
            if r2 < rate2:
                ms += best
                move_skittle_trace(sim_coords, best)
                log.append((f"自分: {best}番成功", dict(sim_coords), ms, os_))
            elif r2 < rate2 + 0.15:
                mm = 0
                hit2 = choose_unintended_hit(best, sim_coords)
                if hit2 is not None:
                    ms += hit2
                    move_skittle_trace(sim_coords, hit2)
                    log.append((f"自分: 得点あり失敗(意図せず{hit2}番)", dict(sim_coords), ms, os_))
            else:
                mm += 1
                log.append((f"自分: 完全ミス({mm}回目)", dict(sim_coords), ms, os_))
                if mm >= 3:
                    log.append(("→ 自分3連続ミスで相手の勝利", dict(sim_coords), ms, os_))
                    break
        else:
            opp_target = random.choice(active_now)
            coord = sim_coords[opp_target]
            opp_dist = max(3.0, min(10.0, round(coord[1] / 0.5) * 0.5))
            opp_rate = OPP_RATE.get(opp_dist, 0.5)
            r3 = random.random()
            if r3 < opp_rate:
                os_ += opp_target
                om = 0
                move_skittle_trace(sim_coords, opp_target)
                log.append((f"相手: {opp_target}番成功", dict(sim_coords), ms, os_))
            elif r3 < opp_rate + 0.15:
                om = 0
                hit3 = choose_unintended_hit(opp_target, sim_coords)
                if hit3 is not None:
                    os_ += hit3
                    move_skittle_trace(sim_coords, hit3)
                    log.append((f"相手: 得点あり失敗(意図せず{hit3}番)", dict(sim_coords), ms, os_))
            else:
                om += 1
                log.append((f"相手: 完全ミス({om}回目)", dict(sim_coords), ms, os_))
                if om >= 3:
                    log.append(("→ 相手3連続ミスで自分の勝利", dict(sim_coords), ms, os_))
                    break

        if ms >= 50 or os_ >= 50:
            break
        my_turn = not my_turn

    return log


def run_simulation(
    my_score, my_miss, opponents,
    skittle_coords, df,
    n_sim=2000
):
    OPP_RATE = {
        3.0:0.70, 3.5:0.65, 4.0:0.60, 4.5:0.55,
        5.0:0.50, 5.5:0.45, 6.0:0.40, 6.5:0.35,
        7.0:0.30, 7.5:0.25, 8.0:0.20, 8.5:0.15,
        9.0:0.12, 9.5:0.10, 10.0:0.08
    }

    base_active = {n: c for n, c in skittle_coords.items() if c is not None}
    if not base_active:
        return {}

    rate_memo = {}

    def get_rate_for(num, coords_dict):
        """現在の配置に基づいてnum番の成功率を計算（キャッシュ付き）"""
        obs = get_obstacle_flags(num, coords_dict)
        d = round(coords_dict[num][1] / 0.5) * 0.5
        d = max(3.0, min(10.0, d))
        key = (d, tuple(sorted(obs.items())))
        if key not in rate_memo:
            rate_memo[key] = get_success_rate(d, obs, df)
        return rate_memo[key]

    def move_skittle(coords_dict, num):
        """num番のスキットルを倒れた位置に移動"""
        if coords_dict.get(num) is None:
            return
        x, y = coords_dict[num]
        dx, dy = get_fall_offset()
        new_x = max(-2.0, min(2.0, x + dx))
        new_y = max(0.1, min(10.0, y + dy))
        coords_dict[num] = (new_x, new_y)

    results = {}

    for target_num in base_active:
        win_count = 0

        for _ in range(n_sim):
            # 各試行ごとに座標をコピー（独立した盤面で進行）
            sim_coords = {n: c for n, c in skittle_coords.items()}

            ms = my_score
            mm = my_miss

            # 自分の1投目
            my_rate = get_rate_for(target_num, sim_coords)
            result = random.random()
            if result < my_rate:
                ms += target_num
                mm = 0
                move_skittle(sim_coords, target_num)
                if ms == 50:
                    win_count += 1
                    continue
                elif ms > 50:
                    ms = 25
            elif result < my_rate + 0.15:
                mm = 0
                hit = choose_unintended_hit(target_num, sim_coords)
                if hit is not None:
                    ms += hit
                    move_skittle(sim_coords, hit)
                    if ms == 50:
                        win_count += 1
                        continue
                    elif ms > 50:
                        ms = 25
            else:
                mm += 1
                if mm >= 3:
                    continue

            # 以降のターン
           # 以降のターン
            MAX_TURNS = 80
            opp_states = [{"score": o["score"], "miss": o["miss"]} for o in opponents]
            turn_idx = 0  # 0=自分、1=相手1、2=相手2...

            for _ in range(MAX_TURNS):
                active_now = [n for n, c in sim_coords.items() if c is not None]
                if not active_now:
                    break

                if turn_idx == 0:
                    # 自分のターン
                    best = min(active_now,
                               key=lambda n: abs(ms + n - 50) if ms + n <= 50
                               else abs(ms + n - 25 - 50))
                    rate2 = get_rate_for(best, sim_coords)

                    r2 = random.random()
                    if r2 < rate2:
                        ms += best
                        mm = 0
                        move_skittle(sim_coords, best)
                        if ms == 50:
                            win_count += 1
                            break
                        elif ms > 50:
                            ms = 25
                    elif r2 < rate2 + 0.15:
                        mm = 0
                        hit2 = choose_unintended_hit(best, sim_coords)
                        if hit2 is not None:
                            ms += hit2
                            move_skittle(sim_coords, hit2)
                            if ms == 50:
                                win_count += 1
                                break
                            elif ms > 50:
                                ms = 25
                    else:
                        mm += 1
                        if mm >= 3:
                            break
                else:
                    # 相手のターン（turn_idx-1番目の相手）
                    opp = opp_states[turn_idx - 1]
                    opp_target = random.choice(active_now)
                    coord = sim_coords[opp_target]
                    opp_dist = round(coord[1] / 0.5) * 0.5
                    opp_dist = max(3.0, min(10.0, opp_dist))
                    opp_rate = OPP_RATE.get(opp_dist, 0.5)

                    r3 = random.random()
                    if r3 < opp_rate:
                        opp["score"] += opp_target
                        opp["miss"] = 0
                        move_skittle(sim_coords, opp_target)
                        if opp["score"] == 50:
                            break
                        elif opp["score"] > 50:
                            opp["score"] = 25
                    elif r3 < opp_rate + 0.15:
                        opp["miss"] = 0
                        hit3 = choose_unintended_hit(opp_target, sim_coords)
                        if hit3 is not None:
                            opp["score"] += hit3
                            move_skittle(sim_coords, hit3)
                            if opp["score"] == 50:
                                break
                            elif opp["score"] > 50:
                                opp["score"] = 25
                    else:
                        opp["miss"] += 1
                        if opp["miss"] >= 3:
                            win_count += 1
                            break

                # 次のターンへ（自分→相手1→相手2→...→自分→...）
                turn_idx = (turn_idx + 1) % (len(opponents) + 1)

        results[target_num] = win_count / n_sim

    return results

# --- ログイン画面 ---
if 'current_player' not in st.session_state:
    st.session_state.current_player = None

if st.session_state.current_player is None:
    st.markdown("""
        <style>
        .stApp {
            background: #FFE4B5;
        }
        div[data-testid="stSelectbox"] div[role="group"] {
            background-color: #FFFFFF !important;
            border: 2px solid #FF6B35 !important;
            border-radius: 12px !important;
        }
        div[data-testid="stSelectbox"] input {
            background-color: #FFFFFF !important;
            color: #1e293b !important;
        }
        div[data-testid="stSelectbox"] button svg {
            fill: #FF6B35 !important;
        }
        [role="listbox"] {
            background-color: #FFFFFF !important;
            border: 2px solid #FF6B35 !important;
        }
        [role="option"] {
            background-color: #FFFFFF !important;
            color: #1e293b !important;
        }
        [role="option"][aria-selected="true"],
        [role="option"]:hover {
            background-color: #FFE4B5 !important;
        }
        div[data-testid="stTextInputRootElement"] {
            background-color: #FFFFFF !important;
            border: 2px solid #FF6B35 !important;
            border-radius: 12px !important;
        }
        div[data-testid="stTextInput"] input {
            background-color: #FFFFFF !important;
            color: #1e293b !important;
        }
        div.stButton > button {
            border-radius: 16px !important;
            font-weight: 700 !important;
            font-size: 16px !important;
            padding: 0.6rem 0.5rem !important;
            border: 2px solid #FF6B35 !important;
            color: #FF6B35 !important;
            background: #FFFFFF !important;
            transition: transform .08s ease, box-shadow .08s ease;
            box-shadow: 0 2px 0 rgba(255,107,53,0.25);
        }
        div.stButton > button:hover {
            background: #FFF1E6 !important;
        }
        div.stButton > button:active {
            transform: translateY(2px);
            box-shadow: none;
        }
        div.stButton > button[kind="primary"] {
            background: #FF6B35 !important;
            color: #FFFFFF !important;
            border: 2px solid #FF6B35 !important;
        }
        div.stButton > button[kind="primary"]:hover {
            background: #FF8156 !important;
        }
        </style>
        """, unsafe_allow_html=True)

    import base64 as _base64
    with open("assets/skittles.png", "rb") as _f:
        _skittles_b64 = _base64.b64encode(_f.read()).decode()
    st.markdown(f"""
        <div style="text-align:center;">
            <img src="data:image/png;base64,{_skittles_b64}" width="110" />
        </div>
        """, unsafe_allow_html=True)

    st.markdown("""
        <div style="text-align:center; padding: 4px 0 20px;">
            <div style="font-size:26px; font-weight:800; color:#1e293b; line-height:1.3;">
                モルック戦術<br>支援アプリ
            </div>
            <div style="font-size:14px; color:#94a3b8; margin-top:4px; font-weight:600;">
                プレイヤーを選んでスタート！
            </div>
        </div>
        """, unsafe_allow_html=True)

    existing_players = db_conn.query(
        "SELECT name FROM players ORDER BY name", ttl=0
    )['name'].tolist()

    if existing_players:
        st.markdown(
            "<div style='font-size:13px; font-weight:700; color:#94a3b8; margin-bottom:8px;'>"
            "👥 登録済みプレイヤー</div>",
            unsafe_allow_html=True,
        )
        selected_player = st.selectbox(
            "登録済みプレイヤー", existing_players,
            index=None, placeholder="選択してください",
            label_visibility="collapsed",
        )
        if st.button("このプレイヤーでログイン", type="primary", use_container_width=True):
            if selected_player:
                st.session_state.current_player = selected_player
                st.rerun()
            else:
                st.warning("プレイヤーを選択してください。")
        st.write("")

    st.markdown(
        "<div style='font-size:13px; font-weight:700; color:#94a3b8; margin: 12px 0 8px;'>"
        "➕ 新しいプレイヤーを追加</div>",
        unsafe_allow_html=True,
    )
    new_player_name = st.text_input("プレイヤー名", label_visibility="collapsed", placeholder="名前を入力")
    if st.button("追加してログイン", type="primary", use_container_width=True):
        name = new_player_name.strip()
        if name:
            with db_conn.session as s:
                s.execute(
                    text("INSERT INTO players (name) VALUES (:name) ON CONFLICT (name) DO NOTHING"),
                    {"name": name}
                )
                s.commit()
            st.session_state.current_player = name
            st.rerun()
        else:
            st.warning("名前を入力してください。")

    st.stop()

# --- メニュー ---
if st.query_params.get("page") == "sim":
    st.session_state['_page'] = "🤖 AI戦術提示シミュレーター"

col_player, col_switch = st.columns([3, 1])
with col_player:
    st.caption(f"👤 プレイヤー: {st.session_state.current_player}")
with col_switch:
    if st.button("切替", use_container_width=True):
        st.session_state.current_player = None
        st.rerun()

page = st.radio(
    "メニューを切り替え",
    ["🎯 投擲データ記録", "🤖 AI戦術提示シミュレーター"],
    horizontal=True,
    index=1 if st.session_state.get('_page') == "🤖 AI戦術提示シミュレーター" else 0
)
st.session_state['_page'] = page

# スマホでの横スクロールを防ぐ（両画面共通）
st.markdown("""
    <style>
    html, body {
        overflow-x: hidden !important;
        max-width: 100vw !important;
    }
    .block-container {
        max-width: 100vw !important;
        overflow-x: hidden !important;
    }
    </style>
    """, unsafe_allow_html=True)

st.divider()

@st.cache_data(ttl=60)
def load_data(player):
    return db_conn.query(
        "SELECT * FROM throw_logs WHERE player = :player", params={"player": player}, ttl=0
    )

def render_swipeable_history(subset_df, max_height=None, key=None):
    """投擲履歴を1行ずつ表示し、左にスワイプすると削除ボタンが出るコンポーネント"""
    if subset_df.empty:
        return

    rows = [
        {
            "id": int(row['id']),
            "timestamp": row['timestamp'],
            "target_label": row['狙った番号'],
            "dist_label": row['距離'],
            "result_label": row['結果'],
        }
        for _, row in subset_df.iterrows()
    ]

    nonce_key = f"_swipe_nonce_{key}"
    if nonce_key not in st.session_state:
        st.session_state[nonce_key] = 0

    deleted_id = _swipe_history_component(
        rows=rows,
        maxHeight=max_height,
        key=f"swipe_{key}_{st.session_state[nonce_key]}",
        default=None,
    )

    if deleted_id:
        with db_conn.session as s:
            s.execute(text("DELETE FROM throw_logs WHERE id = :id"), {"id": int(deleted_id)})
            s.commit()
        st.session_state[nonce_key] += 1
        st.cache_data.clear()
        st.rerun()

# --- 状態管理 ---
if 'obstacles' not in st.session_state:
    st.session_state.obstacles = {
        'nw': False, 'n': False, 'ne': False,
        'w': False,  'e': False,
        'sw': False, 's': False, 'se': False
    }

if 'fullscreen' not in st.session_state:
    st.session_state.fullscreen = False

if 'palette_open' not in st.session_state:
    st.session_state.palette_open = True

if "skittle_m_coords" not in st.session_state:
    st.session_state.skittle_m_coords = {num: None for num in range(1, 13)}

if "opponents" not in st.session_state:
    st.session_state.opponents = [{"score": 0, "miss": 0}]  # デフォルト1人

if 'player_game_state' not in st.session_state:
    st.session_state.player_game_state = {}

def get_player_state():
    """ログイン中のプレイヤーごとに得点状態を分けて保持する"""
    p = st.session_state.current_player
    if p not in st.session_state.player_game_state:
        st.session_state.player_game_state[p] = {
            'my_score_game': 0,
            'my_miss_game': 0,
            'my_total_score': 0,
            'game_message': None,
        }
    return st.session_state.player_game_state[p]

def render_theme_css():
    """全画面共通の配色テーマ（暖色背景＋オレンジのアクセント）"""
    st.markdown("""
        <style>
        .stApp {
            background: #FFE4B5;
        }
        div.stButton > button {
            border-radius: 14px !important;
            font-weight: 700 !important;
            border: 2px solid #FF6B35 !important;
            color: #FF6B35 !important;
            background: #FFFFFF !important;
            transition: transform .08s ease, box-shadow .08s ease;
            box-shadow: 0 2px 0 rgba(255,107,53,0.25);
        }
        div.stButton > button:hover {
            background: #FFF1E6 !important;
        }
        div.stButton > button:active {
            transform: translateY(2px);
            box-shadow: none;
        }
        div.stButton > button[kind="primary"] {
            background: #FF6B35 !important;
            color: #FFFFFF !important;
            border: 2px solid #FF6B35 !important;
        }
        div.stButton > button[kind="primary"]:hover {
            background: #FF8156 !important;
        }
        div.stButton > button:disabled {
            background: #FFFFFF !important;
            border: 2px dashed #E8C99B !important;
            color: #E8C99B !important;
            box-shadow: none !important;
        }
        div[data-testid="stSelectbox"] div[role="group"] {
            background-color: #FFFFFF !important;
            border: 2px solid #FF6B35 !important;
            border-radius: 12px !important;
        }
        div[data-testid="stSelectbox"] input {
            background-color: #FFFFFF !important;
            color: #1e293b !important;
        }
        div[data-testid="stSelectbox"] button svg {
            fill: #FF6B35 !important;
        }
        [role="listbox"] {
            background-color: #FFFFFF !important;
            border: 2px solid #FF6B35 !important;
        }
        [role="option"] {
            background-color: #FFFFFF !important;
            color: #1e293b !important;
        }
        [role="option"][aria-selected="true"],
        [role="option"]:hover {
            background-color: #FFE4B5 !important;
        }
        div[data-testid="stMetric"] {
            background: #FFFFFF;
            border: 2px solid #FF6B35;
            border-radius: 14px;
            padding: 10px 14px;
        }
        div[data-testid="stSlider"] [data-baseweb="slider"] div[role="slider"] {
            background-color: #FF6B35 !important;
        }
        div[data-testid="stSliderTrackFill"], div[data-testid="stSlider"] div[style*="background-color: rgb(255, 75, 75)"] {
            background-color: #FF6B35 !important;
        }
        </style>
        """, unsafe_allow_html=True)

# ==========================================
# 画面1：🎯 投擲データ記録
# ==========================================
if page == "🎯 投擲データ記録":
    render_theme_css()
    st.markdown("""
        <style>
        {
            max-width: 100% !important;
            box-sizing: border-box !important;
        }
        .block-container {
            padding-left: 8px !important;
            padding-right: 8px !important;
            padding-top: 5rem !important;
        }
        .st-key-info_row div[data-testid="stHorizontalBlock"] {
            display: flex !important;
            flex-direction: row !important;
            flex-wrap: nowrap !important;
            gap: 10px !important;
            align-items: flex-start !important;
        }
        .st-key-info_row div[data-testid="stHorizontalBlock"] > div:nth-child(1) {
            flex: 1 1 30% !important;
            max-width: 30% !important;
            min-width: 0px !important;
        }
        .st-key-info_row div[data-testid="stHorizontalBlock"] > div:nth-child(2) {
            flex: 1 1 70% !important;
            max-width: 70% !important;
            min-width: 0px !important;
        }
        div.stButton > button {
            width: 100% !important;
            height: 55px !important;
            padding: 0px !important;
            font-size: 13px !important;
        }
        .st-key-obstacle_grid div.stButton > button {
            aspect-ratio: 1 / 1 !important;
            height: auto !important;
            width: 100% !important;
            font-size: 15px !important;
            font-weight: 800 !important;
        }
        .st-key-obstacle_grid div[data-testid="stHorizontalBlock"] {
            display: flex !important;
            flex-direction: row !important;
            flex-wrap: nowrap !important;
            gap: 10px !important;
            margin-bottom: 10px !important;
        }
        .st-key-obstacle_grid div[data-testid="stHorizontalBlock"] > div {
            flex: 1 1 33.33% !important;
            max-width: 33.33% !important;
            min-width: 0px !important;
        }
        </style>
        """, unsafe_allow_html=True)

    st.title("🎯 投擲データ入力")

    ps = get_player_state()

    # 得点表示
    st.metric("自分の得点", f"{ps['my_score_game']}点")
    st.caption(f"連続ミス：{ps['my_miss_game']}回")
    st.caption(f"累計得点：{ps['my_total_score']}点")

    if ps['game_message']:
        st.info(ps['game_message'])
        if st.button("OK"):
            ps['game_message'] = None
            st.rerun()

    # リセットボタン
    if st.button("🔄 得点をリセット", use_container_width=True):
        ps['my_score_game'] = 0
        ps['my_miss_game'] = 0
        st.rerun()

    st.divider()

    info_row = st.container(key="info_row")
    col_info1, col_info2 = info_row.columns([1, 2])
    with col_info1:
        target_no = st.selectbox("狙う番号", list(range(1, 13)), index=11)
    with col_info2:
        dist = st.slider("距離 (m)", 3.0, 10.0, 3.5, 0.5)

    st.divider()

    st.subheader("障害物配置")
    st.caption("ターゲット周囲の状況をタップ（前＝自分に近い側）")

    keys = [
        ['nw', 'n', 'ne'],
        ['w',  None, 'e'],
        ['sw', 's', 'se']
    ]
    labels = {
        'nw': '左奥', 'n': '奥', 'ne': '右奥',
        'w': '左', 'e': '右',
        'sw': '左前', 's': '手前', 'se': '右前'
    }

    with st.container(key="obstacle_grid"):
        for row in keys:
            cols = st.columns(3, gap="small")
            for i, key in enumerate(row):
                if key is None:
                    cols[i].button("🎯", disabled=True, key="center_target")
                else:
                    is_active = st.session_state.obstacles[key]
                    label = f"🚩 {labels[key]}" if is_active else labels[key]
                    if cols[i].button(label, key=f"btn_{key}"):
                        st.session_state.obstacles[key] = not st.session_state.obstacles[key]
                        st.rerun()

    st.divider()

    success_val = st.radio("結果", ["成功", "得点あり失敗", "完全ミス"], horizontal=True)
    if success_val == "得点あり失敗":
        hit_no = st.selectbox("倒れたスキットル番号", list(range(1, 13)))
    else:
        hit_no = None

    if st.button("記録を保存する", type="primary", use_container_width=True):
        with db_conn.session as s:
            s.execute(text('''INSERT INTO throw_logs
                         (dist, target_no, is_success, n, ne, e, se, s, sw, w, nw, player, timestamp)
                         VALUES (:dist, :target_no, :is_success, :n, :ne, :e, :se, :s, :sw, :w, :nw, :player, :ts)'''),
                      {
                          "dist": dist,
                          "target_no": target_no,
                          "is_success": 1 if success_val == "成功" else (2 if success_val == "得点あり失敗" else 0),
                          "n": int(st.session_state.obstacles['n']),
                          "ne": int(st.session_state.obstacles['ne']),
                          "e": int(st.session_state.obstacles['e']),
                          "se": int(st.session_state.obstacles['se']),
                          "s": int(st.session_state.obstacles['s']),
                          "sw": int(st.session_state.obstacles['sw']),
                          "w": int(st.session_state.obstacles['w']),
                          "nw": int(st.session_state.obstacles['nw']),
                          "player": st.session_state.current_player,
                          "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                      })
            s.commit()

        # 得点計算
        if success_val == "成功":
            ps['my_score_game'] += target_no
            ps['my_miss_game'] = 0
            if ps['my_score_game'] == 50:
                ps['my_total_score'] += 50
                ps['my_score_game'] = 0
                ps['my_miss_game'] = 0
                ps['game_message'] = "🎉 50点ちょうど！このゲームクリア！次のゲームへ"
            elif ps['my_score_game'] > 50:
                ps['my_score_game'] = 25
        elif success_val == "得点あり失敗":
            if hit_no:
                ps['my_score_game'] += hit_no
                ps['my_miss_game'] = 0
                if ps['my_score_game'] == 50:
                    ps['my_total_score'] += 50
                    ps['my_score_game'] = 0
                    ps['my_miss_game'] = 0
                    ps['game_message'] = "🎉 50点ちょうど！このゲームクリア！次のゲームへ"
                elif ps['my_score_game'] > 50:
                    ps['my_score_game'] = 25
        else:
            ps['my_miss_game'] += 1
            if ps['my_miss_game'] >= 3:
                ps['my_total_score'] += ps['my_score_game']
                ps['my_score_game'] = 0
                ps['my_miss_game'] = 0
                ps['game_message'] = "😢 3回連続ミス！このゲーム終了...次のゲームへ"

        for k in st.session_state.obstacles:
            st.session_state.obstacles[k] = False
        st.cache_data.clear()
        st.success("データを保存しました！")
        st.rerun()

    st.subheader("📊 距離別の成功率実績")

    df = load_data(st.session_state.current_player)

    if not df.empty:
        stats_df = df.groupby('dist').apply(
            lambda x: pd.Series({
                'success': (x['is_success'] == 1).sum(),
                'count': len(x)
            })
        ).reset_index()
        stats_df['success_rate'] = (stats_df['success'] / stats_df['count']) * 100
        stats_df['counts_text'] = stats_df['success'].astype(str) + " / " + stats_df['count'].astype(str) + " 回"
        stats_df['dist_label'] = stats_df['dist'].astype(str) + "m"

        import altair as alt
        chart = alt.Chart(stats_df).mark_bar(color="#1f77b4").encode(
            y=alt.Y('dist_label:N', title='投擲距離 (m)', sort=None),
            x=alt.X(
                'success_rate:Q', title='成功率 (%)',
                scale=alt.Scale(domain=[0, 108]),
                axis=alt.Axis(values=list(range(0, 101, 10))),
            ),
            tooltip=[
                alt.Tooltip('dist_label:N', title='距離'),
                alt.Tooltip('success_rate:Q', title='成功率', format='.1f'),
                alt.Tooltip('counts_text:N', title='成功 / 試行回数')
            ]
        ).properties(height=400)
        st.altair_chart(chart, use_container_width=True)

        df_disp = df.copy()
        df_disp['結果'] = df_disp['is_success'].map({1: "○ 成功", 2: "△ 得点あり失敗", 0: "× 完全ミス"})
        df_disp['距離'] = df_disp['dist'].astype(str) + "m"
        df_disp['狙った番号'] = df_disp['target_no'].astype(str) + "番"
        df_disp = df_disp.sort_values(by='timestamp', ascending=False)

        st.markdown("### 📜 最新の投擲履歴（5件）")
        st.caption("行を左にスワイプすると削除ボタンが表示されます（タップで即削除されます）")
        render_swipeable_history(df_disp.head(5), key="recent")

        st.write("")

        export_cols = ['timestamp', '狙った番号', '距離', '結果', 'n', 'ne', 'e', 'se', 's', 'sw', 'w', 'nw']
        actual_cols = [c for c in export_cols if c in df_disp.columns]
        df_csv = df_disp[actual_cols].copy()
        rename_dict = {
            'n': '奥', 'ne': '右奥', 'e': '右', 'se': '右前',
            's': '手前', 'sw': '左前', 'w': '左', 'nw': '左奥'
        }
        df_csv = df_csv.rename(columns=rename_dict)
        try:
            csv_data = df_csv.to_csv(index=False).encode('cp932', errors='ignore')
        except Exception:
            csv_data = df_csv.to_csv(index=False).encode('utf-8-sig')

        if len(df_disp) > 5:
            if st.checkbox("📁 6件目より過去のすべての履歴を表示する"):
                st.markdown("### 📚 過去の投擲履歴（全件）")
                st.caption("行を左にスワイプすると削除ボタンが表示されます（タップで即削除されます）")
                render_swipeable_history(df_disp, max_height=320, key="all")

    else:
        st.info("データを保存すると、ここに自動で成功率のグラフが生成されます。")
        csv_data = b""

    if 'csv_data' in dir():
        st.download_button(
            label="📥 これまでの全投擲データをCSVでダウンロード",
            data=csv_data,
            file_name=f"molkky_throw_logs_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            use_container_width=True
        )

# ==========================================
# 画面2：🤖 AI戦術提示シミュレーター
# ==========================================
else:
    # -------------------------------------------------------
    # 通常モード UI
    # -------------------------------------------------------
    if not st.session_state.fullscreen:
        render_theme_css()
        st.markdown(
            '<h1 style="font-size:28px; line-height:1.3;">🤖 AI戦術提示<br>シミュレーター</h1>',
            unsafe_allow_html=True,
        )
        # 自分のスコア入力
        st.write("**自分**")
        col1, col2 = st.columns(2)
        with col1:
            my_score = st.slider("自分の現在の得点", 0, 50, 0, key="my_score")
        with col2:
            my_miss = st.radio("自分の連続ミス回数", [0, 1, 2], horizontal=True, key="sim_my_miss")

        st.write("---")

        # 相手のスコア入力（動的に増減）
        for i, opp in enumerate(st.session_state.opponents):
            st.write(f"**相手{i+1}**")
            col1, col2, col3 = st.columns([2, 2, 1])
            with col1:
                st.session_state.opponents[i]["score"] = st.slider(
                    f"相手{i+1}の得点", 0, 50, opp["score"], key=f"opp_score_{i}")
            with col2:
                st.session_state.opponents[i]["miss"] = st.radio(
                    f"相手{i+1}の連続ミス回数", [0, 1, 2], horizontal=True,
                    index=opp["miss"], key=f"opp_miss_{i}")
            with col3:
                st.write("")
                st.write("")
                if len(st.session_state.opponents) > 1:
                    if st.button("－", key=f"del_opp_{i}"):
                        st.session_state.opponents.pop(i)
                        st.rerun()

        col_add, _ = st.columns([1, 3])
        with col_add:
            if len(st.session_state.opponents) < 3:  # 最大3人（自分含め4チーム）
                if st.button("＋ 相手を追加"):
                    st.session_state.opponents.append({"score": 0, "miss": 0})
                    st.rerun()

        st.write("")
        st.subheader("🗺️ 全スキットル位置再現マップ")

        col_space, col_btn1, col_btn2 = st.columns([1, 1.5, 1.5])
        with col_btn1:
            if st.button("⛶ 全画面コートで配置", type="primary", use_container_width=True):
                st.session_state.fullscreen = True
                st.rerun()
        with col_btn2:
            if st.button("🔄 ピンをリセット", use_container_width=True):
                st.session_state.skittle_m_coords = {num: None for num in range(1, 13)}
                st.rerun()

        # ========================
        # 通常モード：st_canvas（4m×10m）
        # ========================
        SCALE_UNIT = 24.0
        COURT_WIDTH  = int(4  * SCALE_UNIT)   # 96px
        COURT_HEIGHT = int(10 * SCALE_UNIT)   # 240px
        COURT_START_X = 130
        CENTER_X = COURT_START_X + COURT_WIDTH // 2
        CANVAS_WIDTH  = 320
        CANVAS_HEIGHT = COURT_HEIGHT + 20      # 下に余白20px追加

        emoji_pins = {
            1:"①",2:"②",3:"③",4:"④",5:"⑤",6:"⑥",
            7:"⑦",8:"⑧",9:"⑨",10:"⑩",11:"⑪",12:"⑫"
        }

        court_objects = [
            {"type":"rect","left":10,"top":10,"width":110,"height":COURT_HEIGHT-20,
             "fill":"rgba(0,0,0,0)","stroke":"#94a3b8","strokeWidth":1.5,
             "strokeDashArray":[4,4],"selectable":False,"evented":False,
             "lockMovementX":True,"lockMovementY":True},
            {"type":"rect","left":COURT_START_X,"top":0,
             "width":COURT_WIDTH,"height":COURT_HEIGHT,
             "fill":"rgba(0,0,0,0)","stroke":"#475569","strokeWidth":2,
             "selectable":False,"evented":False,"lockMovementX":True,"lockMovementY":True},
            {"type":"line","x1":CENTER_X,"y1":0,"x2":CENTER_X,"y2":COURT_HEIGHT,
             "stroke":"#cbd5e1","strokeWidth":1.5,"selectable":False,"evented":False,
             "lockMovementX":True,"lockMovementY":True},
            {"type":"line",
             "x1":COURT_START_X,"y1":COURT_HEIGHT-int(3.5*SCALE_UNIT),
             "x2":COURT_START_X+COURT_WIDTH,"y2":COURT_HEIGHT-int(3.5*SCALE_UNIT),
             "stroke":"#f59e0b","strokeWidth":1.5,"selectable":False,"evented":False,
             "lockMovementX":True,"lockMovementY":True},
        ]
        for m in range(1, 10):
            court_objects.append({
                "type":"text","text":f"{m}m",
                "left":COURT_START_X-18,"top":COURT_HEIGHT-int(m*SCALE_UNIT),
                "fontSize":9,"fill":"#94a3b8",
                "originX":"center","originY":"center",
                "selectable":False,"evented":False
            })
        for num in range(1, 13):
            coords = st.session_state.skittle_m_coords[num]
            if coords is not None:
                rx, ry = coords
                px = CENTER_X + rx * SCALE_UNIT
                py = COURT_HEIGHT - ry * SCALE_UNIT
            else:
                col_idx = 0 if num <= 6 else 1
                row_idx = (num - 1) % 6
                px = 30 if col_idx == 0 else 80
                py = 25 + row_idx * 38
            court_objects.append({
                "type":"text","text":emoji_pins[num],
                "left":px,"top":py,
                "fontSize":16,"fontWeight":"bold","fill":"#ef4444",
                "originX":"center","originY":"center",
                "hasControls":False,"hasBorders":False,
                "lockScalingX":True,"lockScalingY":True,"lockRotation":True
            })

         # キャンバス再描画時のちらつきを抑制
        st.markdown("""
        <style>
        canvas { transition: opacity 0.1s; }
        </style>
        """, unsafe_allow_html=True)

        # initial_drawingはcanvas_versionが変わったときだけ更新
        canvas_key = f"normal_court_{st.session_state.get('canvas_version', 0)}"

        canvas_result = st_canvas(
            stroke_width=1, background_color="#f8fafc",
            drawing_mode="transform",
            width=CANVAS_WIDTH, height=CANVAS_HEIGHT,
            initial_drawing={"objects": court_objects},
            key=canvas_key,
        )

        if canvas_result.json_data is not None:
            rev = {v: k for k, v in emoji_pins.items()}
            new_coords = dict(st.session_state.skittle_m_coords)
            changed = False
            for obj in canvas_result.json_data["objects"]:
                if obj.get("type") == "text" and obj.get("text","").strip() in rev:
                    pn = rev[obj["text"].strip()]
                    rx_raw, ry_raw = obj["left"], obj["top"]
                    if rx_raw < 130:
                        if new_coords[pn] is not None:
                            new_coords[pn] = None
                            changed = True
                    else:
                        rx_m = (rx_raw - CENTER_X) / SCALE_UNIT
                        ry_m = (COURT_HEIGHT - ry_raw) / SCALE_UNIT
                        rx = round(rx_m / 0.2) * 0.2
                        ry = round(ry_m / 0.2) * 0.2

                        if ry > 0:
                            new_val = (rx, ry)
                            if new_coords[pn] != new_val:
                                new_coords[pn] = new_val
                                changed = True
            # changedフラグが立った時だけ更新
            if changed:
                st.session_state.skittle_m_coords = new_coords
                st.session_state['canvas_version'] = st.session_state.get('canvas_version', 0) + 1
                st.rerun()

        placed = [(n, c) for n, c in st.session_state.skittle_m_coords.items() if c]
        if placed:
            st.write(f"📍 配置済み（{len(placed)}本）:")
            for pn, (gx, gy) in sorted(placed):
                d = f"左{abs(gx):.2f}m" if gx < 0 else (f"右{gx:.2f}m" if gx > 0 else "中央")
                st.caption(f"📌 {pn}番: 奥{gy:.2f}m・{d}")
        else:
            st.caption("ピンをドラッグして配置してください。")

        st.write("")
        if st.button("🚀 勝利確率を計算する", type="primary", use_container_width=True):
            df = load_data(st.session_state.current_player)
            if df.empty:
                st.warning("投擲データがありません。先にデータを記録してください。")
            elif not any(v for v in st.session_state.skittle_m_coords.values()):
                st.warning("スキットルが配置されていません。マップにピンを置いてください。")
            else:
                with st.spinner("シミュレーション計算中..."):
                    results = run_simulation(
                        my_score=my_score,
                        my_miss=my_miss,
                        opponents=st.session_state.opponents,
                        skittle_coords=st.session_state.skittle_m_coords,
                        df=df,
                        n_sim=2000
                    )

                st.write("各スキットルの成功率:")
                active_tmp = {n: c for n, c in st.session_state.skittle_m_coords.items() if c}
                for n, c in sorted(active_tmp.items()):
                    obs = get_obstacle_flags(n, st.session_state.skittle_m_coords)
                    d = round(c[1] / 0.5) * 0.5
                    r = get_success_rate(d, obs, df)
                    st.caption(f"{n}番: 距離{d}m, 成功率{r:.3f}")

                if results:
                    top3 = sorted(results.items(), key=lambda x: x[1], reverse=True)[:3]
                    st.subheader("🏆 推奨スキットル TOP3")
                    for rank, (num, prob) in enumerate(top3, 1):
                        st.metric(
                            label=f"{rank}位：{num}番スキットル",
                            value=f"{prob*100:.1f}%",
                            help=f"このスキットルを狙った場合の勝利確率"
                        )

                    # ↓座標変化のトレース表示（デバッグ用）
                    st.write("---")
                    st.write("🔍 1試行の座標変化（デバッグ用）:")
                    trace_log = trace_one_simulation(
                        top3[0][0], my_score, my_miss,
                        st.session_state.opponents[0]["score"],
                        st.session_state.opponents[0]["miss"],
                        st.session_state.skittle_m_coords, df
                    )
                    for label, coords_snapshot, ms_val, os_val in trace_log:
                        st.caption(f"**{label}** (自分{ms_val}点・相手{os_val}点)")
                        active_str = ", ".join(
                            f"{n}番({c[0]:.2f},{c[1]:.2f})"
                            for n, c in sorted(coords_snapshot.items()) if c
                        )
                        st.caption(active_str)
    # -------------------------------------------------------
    # 全画面モード：declare_component で postMessage 経由のやりとり
    # スライドアウトパレット（右エッジ）＋ ドラッグ&ドロップ
    # -------------------------------------------------------
    else:
        # 現在の座標をコンポーネントの初期値として渡す
        init_coords = []
        for num in range(1, 13):
            c = st.session_state.skittle_m_coords[num]
            if c:
                init_coords.append({"n": num, "x": c[0], "y": c[1]})

        st.markdown("""
        <style>
        header[data-testid="stHeader"],
        section[data-testid="stSidebar"],
        div[data-testid="stToolbar"],
        footer, #MainMenu { display:none !important; }
        .main .block-container { padding:0 !important; max-width:100% !important; }
        iframe:first-of-type {
            position:fixed !important; inset:0 !important;
            width:100vw !important; height:100dvh !important;
            border:none !important; z-index:100 !important;
        }
        iframe:not(:first-of-type) {
            position:fixed !important;
            width:0 !important; height:0 !important;
            top:-9999px !important; left:-9999px !important;
            pointer-events:none !important;
            border:none !important;
        }
        </style>
        """, unsafe_allow_html=True)

        if 'fullscreen_court_nonce' not in st.session_state:
            st.session_state.fullscreen_court_nonce = 0

        exit_result = _fullscreen_court_component(
            coords=init_coords,
            key=f"fullscreen_court_{st.session_state.fullscreen_court_nonce}",
        )

        if exit_result is not None:
            new_coords = {num: None for num in range(1, 13)}
            for item in exit_result:
                n = item["n"]
                if 1 <= n <= 12:
                    new_coords[n] = (item["x"], item["y"])
            st.session_state.skittle_m_coords = new_coords
            st.session_state.fullscreen = False
            st.session_state['_page'] = "🤖 AI戦術提示シミュレーター"
            st.session_state['canvas_version'] = st.session_state.get('canvas_version', 0) + 1
            st.session_state.fullscreen_court_nonce += 1
            st.rerun()
