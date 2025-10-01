import discord
import os
import json
import random
import tempfile
import base64 # <== Base64デコードを追加
from discord import app_commands
from discord.ext import commands
from flask import Flask
from threading import Thread
from multiprocessing import current_process
from datetime import datetime, timedelta, timezone

# Firebase/Firestore関連のインポート
import firebase_admin
from firebase_admin import credentials, firestore

# Flaskのアプリケーションインスタンスを作成（gunicornが実行するWebサーバー）
app = Flask(__name__)

# Firestore接続とBotの状態管理のためのグローバル変数
db = None
last_status_updates = {}
tz_jst = timezone(timedelta(hours=9)) # 日本時間 (JST)

# Botクライアントの定義
class StatusTrackerBot(commands.Bot):
    # ... (この部分は変更なし) ...
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.app_id = os.getenv("__app_id", "default-app-id")
        self.collection_path = f'artifacts/{self.app_id}/public/data/user_status'

    async def on_ready(self):
        print('---------------------------------')
        print(f'Botがログインしました: {self.user.name}')
        print('Botはサーバーのユーザー活動時間を記録します。')
        print('---------------------------------')
        
        try:
            for guild in self.guilds:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            print("スラッシュコマンド同期完了。")
        except Exception as e:
            print(f"スラッシュコマンド同期エラー: {e}")
            
        now = datetime.now(tz_jst)
        for guild in self.guilds:
            for member in guild.members:
                if member.id != self.user.id:
                    status_key = str(member.status)
                    last_status_updates[member.id] = (status_key, now)

    async def on_presence_update(self, before, after):
        if after.id == self.user.id or db is None:
            return

        user_id = after.id
        doc_ref = db.collection(self.collection_path).document(str(user_id))
        now = datetime.now(tz_jst)
        current_status_key = str(after.status)

        if user_id in last_status_updates:
            prev_status_key, prev_time = last_status_updates[user_id]
        else:
            prev_status_key = str(before.status) if before.status else 'offline'
            prev_time = now

        if current_status_key == prev_status_key:
            return

        duration = (now - prev_time).total_seconds()
        field_name = f'{prev_status_key}_seconds'
        date_field_name = f'{now.strftime("%Y-%m-%d")}_{field_name}'

        if duration > 0:
            doc_ref.set({
                field_name: firestore.Increment(duration),
                date_field_name: firestore.Increment(duration),
                'last_updated': now
            }, merge=True)

        last_status_updates[user_id] = (current_status_key, now)

# -----------------
# レポート表示用のヘルパー関数 (更新箇所)
# -----------------
def format_time(seconds: float) -> str:
    """秒数（float）を HH時間 MM分 SS秒 の形式にフォーマットする (ミリ秒表示対応)"""
    if seconds < 0:
        return f"({format_time(abs(seconds))})"
        
    total_seconds_int = int(seconds)
    
    hours, remainder = divmod(total_seconds_int, 3600)
    minutes, seconds_int = divmod(remainder, 60)
    
    # 小数点以下の秒数を取得
    milliseconds = seconds - total_seconds_int
    
    parts = []
    if hours > 0:
        parts.append(f"{hours}時間")
    if minutes > 0:
        parts.append(f"{minutes}分")
    
    # 秒数とミリ秒を表示
    if seconds_int > 0 or milliseconds > 0 or not parts:
        # 秒（整数部） + 小数点以下2桁まで
        formatted_seconds = f"{seconds_int + milliseconds:.2f}秒"
        parts.append(formatted_seconds)
        
    return " ".join(parts)

def get_status_emoji(status):
    if status == 'online': return '🟢 オンライン'
    if status == 'idle': return '🌙 退席中'
    if status == 'dnd': return '🔴 取り込み中'
    if status == 'offline': return '⚫ オフライン'
    return status.capitalize()

async def get_user_report_data(member: discord.Member, db, collection_path, days=7):
    doc_ref = db.collection(collection_path).document(str(member.id))
    doc = doc_ref.get()

    if not doc.exists:
        return None

    data = doc.to_dict()
    now = datetime.now(tz_jst)
    statuses = ['online', 'idle', 'dnd', 'offline']
    
    total_sec = 0
    online_sec = 0
    offline_sec = 0
    user_data = {}

    for status in statuses:
        status_total_sec = 0
        for i in range(days):
            date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            field = f'{date}_{status}_seconds'
            status_total_sec += data.get(field, 0)
        
        user_data[status] = status_total_sec
        total_sec += status_total_sec
        
        # オンライン/オフライン時間の集計
        if status in ['online', 'idle', 'dnd']:
            online_sec += status_total_sec
        elif status == 'offline':
            offline_sec += status_total_sec


    user_data['total'] = total_sec
    user_data['online_time_s'] = online_sec
    user_data['offline_time_s'] = offline_sec
    
    return user_data

async def send_user_report_embed(interaction: discord.Interaction, member: discord.Member, user_data: dict, days: int):
    
    # 活動時間の集計
    online_time = user_data.get('online_time_s', 0)
    offline_time = user_data.get('offline_time_s', 0)
    total_sec = online_time + offline_time
    
    if total_sec == 0:
        await interaction.followup.send(f"⚠️ **{member.display_name}** さんの過去 {days} 日間の活動記録は見つかりませんでした。")
        return

    # フォーマットされた時間
    total_formatted = format_time(total_sec)
    online_formatted = format_time(online_time)
    offline_formatted = format_time(offline_time)
    
    embed = discord.Embed(
        title=f"⏳ {member.display_name} さんの活動時間レポート",
        description=f"集計期間: 過去 **{days}** 日間",
        color=member.color if member.color != discord.Color.default() else discord.Color.blue()
    )
    
    embed.set_thumbnail(url=member.display_avatar.url)

    # 1. 合計活動時間 (一番上に目立つように)
    embed.add_field(
        name="📊 合計活動時間",
        value=f"**{total_formatted}**",
        inline=False 
    )
    
    # 2. オンライン活動時間 (online, idle, dnd の合計)
    embed.add_field(
        name="💻 オンライン活動時間",
        value=f"**{online_formatted}**",
        inline=True
    )
    
    # 3. オフライン時間 (offline)
    embed.add_field(
        name="💤 オフライン時間",
        value=f"{offline_formatted}",
        inline=True
    )
    
    # 4. ステータス別 内訳 (詳細)
    statuses = ['online', 'idle', 'dnd', 'offline']
    status_field_value = []
    
    for status in statuses:
        sec = user_data.get(status, 0)
        if total_sec > 0:
            percentage = (sec / total_sec) * 100
        else:
            percentage = 0
            
        status_field_value.append(
            f"{get_status_emoji(status)}: {format_time(sec)} ({percentage:.1f}%)"
        )

    embed.add_field(
        name="📌 ステータス詳細内訳",
        value="\n".join(status_field_value),
        inline=False
    )
    
    embed.set_footer(text=f"レポート生成時刻: {datetime.now(tz_jst).strftime('%Y/%m/%d %H:%M:%S JST')}")
    await interaction.followup.send(embed=embed)


# -----------------
# Firestore初期化関数 (Base64デコードと一時ファイル処理)
# -----------------
def init_firestore():
    global db
    if db is not None:
        return db

    # Base64エンコードされた設定文字列を環境変数から取得
    base64_config = os.getenv("__firebase_config")
    
    if not base64_config:
        print("致命的エラー: __firebase_config 環境変数が設定されていません。")
        return None

    temp_file_path = None
    try:
        # 1. Base64文字列をデコードし、JSONバイトデータを取得
        json_bytes = base64.b64decode(base64_config)
        json_str = json_bytes.decode('utf-8')
        
        # 2. 一時ファイルを作成し、デコードしたJSONを書き込む
        with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8') as temp_file:
            temp_file.write(json_str)
            temp_file_path = temp_file.name

        # 3. 認証を実行: credentials.Certificate() に一時ファイルのパスを渡す
        cred = credentials.Certificate(temp_file_path)
        
        # 4. Firebaseアプリを初期化
        firebase_admin.initialize_app(cred)
        
        db = firestore.client()
        print("Firestore接続完了。")
        return db
        
    except Exception as e:
        print(f"Firestore初期化に失敗しました。認証情報（__firebase_config）を確認してください: {e}")
        # Base64デコードやJSON解析エラーの詳細を追記
        print("エラー詳細: Base64エンコードされたJSON文字列が不完全、または不正な可能性があります。")
        return None
    
    finally:
        # 5. 認証後、一時ファイルを削除
        if temp_file_path and os.path.exists(temp_file_path):
             os.remove(temp_file_path)


# -----------------
# Discord Bot本体の起動関数 (変更なし)
# -----------------
def run_discord_bot():
    if current_process().name != 'MainProcess':
        print(f"非メインプロセス ({current_process().name}) です。Botは起動しません。")
        return

    if init_firestore() is None:
        print("Botはデータベース接続なしで起動できません。")
        return

    TOKEN = os.getenv("DISCORD_TOKEN")
    
    intents = discord.Intents.default()
    intents.members = True
    intents.presences = True
    intents.message_content = True

    bot = StatusTrackerBot(command_prefix='!', intents=intents)

    # ====================================================================
    # 📌 チャンネルIDの設定場所
    # 
    # 自動レポートなどを送りたいチャンネルのIDをここに設定してください。
    # ここでは例として None に設定していますが、実際のIDに置き換えてください。
    REPORT_CHANNEL_ID = "1422893472599248977"
    # REPORT_CHANNEL_ID = os.getenv("REPORT_CHANNEL_ID", None) # 環境変数から取得する場合
    # REPORT_CHANNEL_ID = 123456789012345678 # ハードコードする場合
    # ====================================================================

    @bot.tree.command(name="mytime", description="指定したユーザーの過去7日間のオンライン時間をレポートします。")
    @app_commands.describe(member='活動時間を知りたいサーバーメンバー')
    async def mytime_command(interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()
        
        user_data = await get_user_report_data(member, db, bot.collection_path, days=7)
        
        await send_user_report_embed(interaction, member, user_data, 7)
    
    # チャンネルIDの使用例を示すテストコマンド
    @bot.tree.command(name="send_report_test", description="設定されたチャンネルへテストレポートを送信します。")
    async def send_report_test_command(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if REPORT_CHANNEL_ID is None:
            await interaction.followup.send("⚠️ レポート送信先チャンネルIDが設定されていません。", ephemeral=True)
            return

        try:
            # チャンネルID (文字列) を整数に変換してチャンネルオブジェクトを取得
            channel = bot.get_channel(int(REPORT_CHANNEL_ID)) 
            if channel:
                test_embed = discord.Embed(
                    title="📝 テストレポート",
                    description="これは設定されたチャンネルへのテスト送信です。\nこの機能が今後、日次レポートなどに使用されます。",
                    color=discord.Color.green()
                )
                await channel.send(embed=test_embed)
                await interaction.followup.send(f"✅ テストレポートをチャンネルID: `{REPORT_CHANNEL_ID}` のチャンネルに送信しました。", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ チャンネルID `{REPORT_CHANNEL_ID}` が見つからないか、Botにアクセス権限がありません。", ephemeral=True)
        except Exception as e:
            print(f"レポート送信エラー: {e}")
            await interaction.followup.send("エラーが発生しました。", ephemeral=True)


    if TOKEN:
        try:
            bot.run(TOKEN)
        except Exception as e:
            print(f"Discord Bot 起動失敗: {e}")
    else:
        print("エラー: Botトークンが設定されていません。")

# -----------------
# Webサーバーのエンドポイント (変更なし)
# -----------------
@app.route('/')
def home():
    if current_process().name == 'MainProcess':
        if not hasattr(app, 'bot_thread_started'):
            app.bot_thread_started = True
            print("Webアクセスを検知。Discord Botの起動を試みます...")
            
            Thread(target=run_discord_bot).start()
            
            return "Discord Bot is initializing... (Please check Discord in 10 seconds)"
        else:
            return "Bot is alive!"
    else:
        return "Bot worker is alive (Sub-process)"
