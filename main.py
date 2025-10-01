import discord
import os
import json
import random
import io # 新しくインポートを追加
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
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.app_id = os.getenv("__app_id", "default-app-id")
        # Public Data (共有データ) として保存
        self.collection_path = f'artifacts/{self.app_id}/public/data/user_status'

    async def on_ready(self):
        print('---------------------------------')
        print(f'Botがログインしました: {self.user.name}')
        print('Botはサーバーのユーザー活動時間を記録します。')
        print('---------------------------------')
        
        # スラッシュコマンドの同期
        try:
            # Botが参加している全てのギルド(サーバー)にコマンドを同期
            for guild in self.guilds:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            print("スラッシュコマンド同期完了。")
        except Exception as e:
            print(f"スラッシュコマンド同期エラー: {e}")
        
        # 起動時のステータス初期記録
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

        # 時間差を計算し、データベースに記録
        duration = (now - prev_time).total_seconds()
        
        # 記録するフィールド名 (例: 'online_seconds', 'idle_seconds')
        field_name = f'{prev_status_key}_seconds'
        
        # 日付フィールド (例: '2025-10-01_online_seconds')
        date_field_name = f'{now.strftime("%Y-%m-%d")}_{field_name}'

        # Firestoreにアトミックな加算操作で時間を記録
        if duration > 0:
            doc_ref.set({
                field_name: firestore.Increment(duration),
                date_field_name: firestore.Increment(duration),
                'last_updated': now
            }, merge=True)
            # print(f"記録: {after.display_name} | {prev_status_key}で {duration:.2f}秒") # ログが多い場合はコメントアウト推奨

        # 状態を更新
        last_status_updates[user_id] = (current_status_key, now)

# -----------------
# Firestore初期化関数
# -----------------
def init_firestore():
    global db
    if db is not None:
        return db

    # Render環境変数からFirebase設定をロード
    firebase_config_str = os.getenv("__firebase_config")
    
    if not firebase_config_str:
        print("致命的エラー: __firebase_config 環境変数が設定されていません。")
        return None

    try:
        # JSON文字列をメモリ上のファイルのように扱います (io.StringIOを使用)
        # これにより、環境変数設定時の予期しない改行やエンコードの問題を回避し、
        # credentials.Certificate() が要求する形式で情報を渡します。
        
        # 重要なステップ: JSON文字列を読み込み、認証情報として直接使用
        cred = credentials.Certificate(io.StringIO(firebase_config_str))
        
        # Firebaseアプリを初期化
        firebase_admin.initialize_app(cred)
        
        db = firestore.client()
        print("Firestore接続完了。")
        return db
    except Exception as e:
        # 認証情報が間違っている、またはJSON文字列に問題がある場合のエラーログ
        print(f"Firestore初期化に失敗しました。認証情報（__firebase_config）を確認してください: {e}")
        return None

# ... その他のコードはそのまま ...

# -----------------
# レポート生成ヘルパー関数
# -----------------
def format_time(seconds):
    if seconds < 0: seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02}時間 {m:02}分 {s:02}秒"

def get_status_emoji(status):
    if status == 'online': return '🟢 オンライン'
    if status == 'idle': return '🌙 退席中'
    if status == 'dnd': return '🔴 取り込み中'
    if status == 'offline': return '⚫ オフライン'
    return status.capitalize()

async def get_user_report_data(member: discord.Member, db, collection_path, days=7):
    # ユーザーデータをFirestoreから取得
    doc_ref = db.collection(collection_path).document(str(member.id))
    doc = doc_ref.get()

    if not doc.exists:
        return None

    data = doc.to_dict()
    now = datetime.now(tz_jst)
    statuses = ['online', 'idle', 'dnd', 'offline']
    
    total_sec = 0
    user_data = {}

    for status in statuses:
        status_total_sec = 0
        # 過去7日間の日付フィールドを集計
        for i in range(days):
            date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            field = f'{date}_{status}_seconds'
            status_total_sec += data.get(field, 0)
        
        user_data[status] = status_total_sec
        total_sec += status_total_sec

    user_data['total'] = total_sec
    return user_data

async def send_user_report_embed(interaction: discord.Interaction, member: discord.Member, user_data: dict, days: int):
    # データが空の場合はメッセージを送信して終了
    if not user_data or user_data['total'] == 0:
        await interaction.followup.send(f"⚠️ **{member.display_name}** さんの過去 {days} 日間の活動記録は見つかりませんでした。")
        return

    total_sec = user_data['total']
    total_formatted = format_time(total_sec)
    
    embed = discord.Embed(
        title=f"⏳ {member.display_name} さんの活動時間レポート",
        description=f"集計期間: 過去 **{days}** 日間（合計: **{total_formatted}**）",
        color=member.color if member.color != discord.Color.default() else discord.Color.blue()
    )
    
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"レポート生成時刻: {datetime.now(tz_jst).strftime('%Y/%m/%d %H:%M:%S JST')}")

    # 各ステータスの時間と割合を計算し、フィールドに追加
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
        name="📊 ステータス別 内訳",
        value="\n".join(status_field_value),
        inline=False
    )
    
    await interaction.followup.send(embed=embed)


# -----------------
# Discord Bot本体の起動関数
# -----------------
def run_discord_bot():
    # 多重起動防止チェック
    if current_process().name != 'MainProcess':
        print(f"非メインプロセス ({current_process().name}) です。Botは起動しません。")
        return

    # Firestoreの初期化
    db = init_firestore()
    if db is None:
        print("Botはデータベース接続なしで起動できません。")
        return

    TOKEN = os.getenv("DISCORD_TOKEN") 
    
    # 必要なインテントを設定
    intents = discord.Intents.default()
    intents.members = True 
    intents.presences = True 
    intents.message_content = True 

    # Botインスタンスを初期化
    bot = StatusTrackerBot(command_prefix='!', intents=intents)

    # /mytime コマンドの定義
    @bot.tree.command(name="mytime", description="指定したユーザーの過去7日間のオンライン時間をレポートします。")
    @app_commands.describe(member='活動時間を知りたいサーバーメンバー')
    async def mytime_command(interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer() # 処理に時間がかかることをDiscordに通知
        
        user_data = await get_user_report_data(member, db, bot.collection_path, days=7)
        
        # レポート送信
        await send_user_report_embed(interaction, member, user_data, 7)
    
    # Botの実行
    if TOKEN:
        try:
            bot.run(TOKEN)
        except Exception as e:
            print(f"Discord Bot 起動失敗: {e}")
    else:
        print("エラー: Botトークンが設定されていません。")

# -----------------
# Webサーバーのエンドポイント (gunicornがアクセスする場所)
# -----------------
@app.route('/')
def home():
    # 多重起動防止ロジック
    if current_process().name == 'MainProcess':
        if not hasattr(app, 'bot_thread_started'):
            app.bot_thread_started = True
            print("Webアクセスを検知。Discord Botの起動を試みます...")
            
            # Botを別スレッドで起動
            Thread(target=run_discord_bot).start()
            
            return "Discord Bot is initializing... (Please check Discord in 10 seconds)"
        else:
            return "Bot is alive!"
    else:
        return "Bot worker is alive (Sub-process)"
