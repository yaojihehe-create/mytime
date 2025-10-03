import discord
import os
import json
import tempfile
import base64
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask
from threading import Thread
from multiprocessing import current_process
from datetime import datetime, timedelta, timezone, time 
import asyncio

# Firebase/Firestore関連のインポート
import firebase_admin
from firebase_admin import credentials, firestore

# Flaskのアプリケーションインスタンスを作成（gunicornが実行するWebサーバー）
app = Flask(__name__)

# Firestore接続とBotの状態管理のためのグローバル変数
db = None
last_status_updates = {}
tz_jst = timezone(timedelta(hours=9)) # 日本時間 (JST)

# 📌 修正点 1: コマンドを強制同期するターゲットサーバーIDを設定 (必須!)
# ----------------------------------------------------
# !!! ここをあなたのサーバーIDに置き換えてください (数字のみ) !!!
# ----------------------------------------------------
TARGET_GUILD_ID = 0 # 例: 123456789012345678 
# ----------------------------------------------------

# Botクライアントの定義
class StatusTrackerBot(commands.Bot):
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.app_id = os.getenv("__app_id", "default-app-id")
        self.collection_path = f'artifacts/{self.app_id}/public/data/user_status'
        self.config_doc_ref = None
        self.report_channel_id = None
        # ターゲットギルドのオブジェクトを定義
        self.target_guild_object = discord.Object(id=TARGET_GUILD_ID)

    async def _initialize_db_references(self):
        """dbが初期化された後、ドキュメント参照を設定する"""
        global db
        if db is not None and self.config_doc_ref is None:
            self.config_doc_ref = db.collection(f'artifacts/{self.app_id}/public/data/bot_config').document('settings')
            return True
        return False

    async def _load_config(self):
        """FirestoreからレポートチャンネルIDをロードする"""
        if not await self._initialize_db_references():
            return False

        try:
            doc = await asyncio.to_thread(self.config_doc_ref.get)
            if doc.exists and 'report_channel_id' in doc.to_dict():
                self.report_channel_id = doc.to_dict()['report_channel_id']
                print(f"FirestoreからレポートチャンネルIDをロード: {self.report_channel_id}")
                return True
            else:
                print("FirestoreにレポートチャンネルIDが見つかりませんでした。")
                return False
        except Exception as e:
            print(f"設定ロード中にエラーが発生しました: {e}")
            return False

    async def _save_config(self, channel_id: int):
        """FirestoreにレポートチャンネルIDを保存する"""
        if not await self._initialize_db_references():
            print("エラー: データベース参照が未設定のため、設定を保存できません。")
            return False

        try:
            await asyncio.to_thread(self.config_doc_ref.set, 
                                    {'report_channel_id': channel_id}, 
                                    merge=True)
            self.report_channel_id = channel_id
            return True
        except Exception as e:
            print(f"設定保存中にエラーが発生しました: {e}")
            return False

    async def on_ready(self):
        print('---------------------------------')
        print(f'Botがログインしました: {self.user.name}')
        
        # 1. データベース設定のロード
        await self._load_config()

        # 2. コマンドの強制同期
        try:
            print(f"--- ターゲットサーバー ({TARGET_GUILD_ID}) への強制同期処理開始 ---")
            
            # ターゲットギルドにコマンドを同期
            # 注意: Discord APIの遅延により、数分かかる場合があります。
            await self.tree.sync(guild=self.target_guild_object)
            
            print(f"--- ターゲットサーバーへのコマンド同期完了 ---")

        except Exception as e:
            # Botの起動自体は止めないが、エラーログを出力
            print(f"スラッシュコマンド同期中のエラー: {e}")
            
        # 3. 記録漏れを防ぐための初期ステータス記録
        now = datetime.now(tz_jst)
        print("ユーザーの初期ステータスを取得しています...")
        target_guild = self.get_guild(TARGET_GUILD_ID)
        
        if target_guild:
            await target_guild.chunk() # メンバーキャッシュを強制的に取得
            for member in target_guild.members:
                if member.bot or member.id in last_status_updates:
                    continue
                
                status_key = str(member.status)
                last_status_updates[member.id] = (status_key, now)
        else:
             print(f"警告: ターゲットサーバーID {TARGET_GUILD_ID} のサーバーが見つかりません。Botがそのサーバーに参加しているか確認してください。")

        print("初期ステータス記録完了。")

        # 4. 定期タスクの開始
        if self.report_channel_id is not None:
            self.daily_report.start()
            print(f"日次レポートタスクを開始しました。送信先: {self.report_channel_id}")
        else:
            print("レポートチャンネルIDが未設定のため、自動送信をスキップします。/set_report_channelで設定してください。")
            
        print('---------------------------------')

    async def on_presence_update(self, before, after):
        # Bot自身、またはデータベースが未接続の場合はスキップ
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

        # ステータスが変わっていない場合は処理を終了
        if current_status_key == prev_status_key:
            return

        duration = (now - prev_time).total_seconds()
        field_name = f'{prev_status_key}_seconds'
        
        # 状態変更が日をまたいだ場合の処理 (簡略化のため、ここでは prev_time の日付を使用)
        date_field_name = f'{prev_time.strftime("%Y-%m-%d")}_{field_name}'

        if duration > 0:
            await asyncio.to_thread(doc_ref.set, {
                field_name: firestore.Increment(duration),
                date_field_name: firestore.Increment(duration),
                'last_updated': now
            }, merge=True) 

        last_status_updates[user_id] = (current_status_key, now)
        
    # ----------------------------------------------------
    # 日次レポートタスク (毎日 JST 00:00 実行)
    # ----------------------------------------------------
    @tasks.loop(time=time(0, 0, tzinfo=tz_jst)) 
    async def daily_report(self):
        await self._load_config() 
        
        if not self.is_ready() or db is None or self.report_channel_id is None:
            return

        report_channel = self.get_channel(self.report_channel_id)
        if not report_channel:
            return

        print("--- 日次レポート処理開始 (JST 00:00) ---")

        # ターゲットギルドのみを処理
        target_guild = self.get_guild(TARGET_GUILD_ID)
        if target_guild:
            days = 1 
            
            for member in target_guild.members:
                if member.bot:
                    continue
                
                user_data = await get_user_report_data(member, db, self.collection_path, days=days)
                
                if not user_data or user_data.get('total', 0) == 0:
                    continue

                online_time = user_data.get('online_time_s', 0)
                offline_time = user_data.get('offline_time_s', 0)
                total_sec = online_time + offline_time
                
                total_formatted = format_time(total_sec)
                online_formatted = format_time(online_time)
                
                embed = discord.Embed(
                    title=f"📅 {member.display_name} さんの日次レポート",
                    description=f"集計期間: **昨日（1日間）**\n📊 **合計活動時間: {total_formatted}**",
                    color=member.color if member.color != discord.Color.default() else discord.Color.blue()
                )
                embed.set_thumbnail(url=member.display_avatar.url)

                embed.add_field(name="💻 オンライン活動時間", value=online_formatted, inline=True)
                embed.add_field(name="💤 オフライン時間", value=format_time(offline_time), inline=True)
                
                embed.set_footer(text=f"レポート生成時刻: {datetime.now(tz_jst).strftime('%Y/%m/%d %H:%M:%S JST')}")

                try:
                    await report_channel.send(embed=embed)
                    await asyncio.sleep(0.5) 
                except Exception as e:
                    print(f"レポート送信失敗 (ユーザーID: {member.id}): {e}")

        print("--- 日次レポート処理完了 ---")
        
    @daily_report.before_loop
    async def before_daily_report(self):
        await self.wait_until_ready()


# -----------------
# ヘルパー関数 (変更なし)
# -----------------
def format_time(seconds: float) -> str:
    # ... (省略: 前のコードと同じ) ...
    if seconds < 0:
        return f"({format_time(abs(seconds))})"
        
    total_seconds_int = int(seconds)
    
    hours, remainder = divmod(total_seconds_int, 3600)
    minutes, seconds_int = divmod(remainder, 60)
    
    milliseconds = seconds - total_seconds_int
    
    parts = []
    if hours > 0:
        parts.append(f"{hours}時間")
    if minutes > 0:
        parts.append(f"{minutes}分")
    
    if seconds_int > 0 or milliseconds > 0 or not parts:
        formatted_seconds = f"{seconds_int + milliseconds:.2f}秒"
        parts.append(formatted_seconds)
        
    return " ".join(parts)

def get_status_emoji(status):
    # ... (省略: 前のコードと同じ) ...
    if status == 'online': return '🟢 オンライン'
    if status == 'idle': return '🌙 退席中'
    if status == 'dnd': return '🔴 取り込み中'
    if status == 'offline': return '⚫ オフライン'
    return status.capitalize()

async def get_user_report_data(member: discord.Member, db, collection_path, days=7):
    # ... (省略: 前のコードと同じ) ...
    doc_ref = db.collection(collection_path).document(str(member.id))
    doc = await asyncio.to_thread(doc_ref.get)

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
        
        if status in ['online', 'idle', 'dnd']:
            online_sec += status_total_sec
        elif status == 'offline':
            offline_sec += status_total_sec


    user_data['total'] = total_sec
    user_data['online_time_s'] = online_sec
    user_data['offline_time_s'] = offline_sec
    
    return user_data

async def send_user_report_embed(interaction: discord.Interaction, member: discord.Member, user_data: dict, days: int):
    # ... (省略: 前のコードと同じ) ...
    online_time = user_data.get('online_time_s', 0)
    offline_time = user_data.get('offline_time_s', 0)
    total_sec = online_time + offline_time
    
    if total_sec == 0:
        await interaction.followup.send(f"⚠️ **{member.display_name}** さんの過去 {days} 日間の活動記録は見つかりませんでした。")
        return

    total_formatted = format_time(total_sec)
    online_formatted = format_time(online_time)
    offline_formatted = format_time(offline_time)
    
    embed = discord.Embed(
        title=f"⏳ {member.display_name} さんの活動時間レポート",
        description=f"集計期間: 過去 **{days}** 日間",
        color=member.color if member.color != discord.Color.default() else discord.Color.blue()
    )
    
    embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(
        name="📊 合計活動時間",
        value=f"**{total_formatted}**",
        inline=False 
    )
    
    embed.add_field(
        name="💻 オンライン活動時間",
        value=f"**{online_formatted}**",
        inline=True
    )
    
    embed.add_field(
        name="💤 オフライン時間",
        value=f"{offline_formatted}",
        inline=True
    )
    
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
# Firestore初期化関数 (変更なし)
# -----------------
def init_firestore():
    global db
    if db is not None:
        return db

    base64_config = os.getenv("__firebase_config")
    
    if not base64_config:
        print("致命的エラー: __firebase_config 環境変数が設定されていません。")
        print("Botはデータベース接続なしで起動できません。")
        return None 

    temp_file_path = None
    try:
        json_bytes = base64.b64decode(base64_config)
        json_str = json_bytes.decode('utf-8')
        
        with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8') as temp_file:
            temp_file.write(json_str)
            temp_file_path = temp_file.name

        cred = credentials.Certificate(temp_file_path)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        
        db = firestore.client()
        print("Firestore接続完了。")
        return db
        
    except Exception as e:
        print(f"Firestore初期化に失敗しました。認証情報（__firebase_config）を確認してください: {e}")
        print("エラー詳細: Base64エンコードされたJSON文字列が不完全、または不正な可能性があります。")
        return None
    
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
             os.remove(temp_file_path)


# -----------------
# Discord Bot本体の起動関数
# -----------------
def run_discord_bot():
    if current_process().name != 'MainProcess':
        print(f"非メインプロセス ({current_process().name}) です。Botは起動しません。")
        return

    if init_firestore() is None:
        return 

    TOKEN = os.getenv("DISCORD_TOKEN")
    
    intents = discord.Intents.default()
    intents.members = True
    intents.presences = True
    intents.message_content = True

    bot = StatusTrackerBot(command_prefix='!', intents=intents)

    # 📌 コマンド定義: TARGET_GUILD_IDにのみ適用される
    guild_id_object = discord.Object(id=TARGET_GUILD_ID)

    @bot.tree.command(name="set_report_channel", description="日次レポートの送信先チャンネルを設定します。", guild=guild_id_object)
    @app_commands.describe(channel='レポートを送信するテキストチャンネル')
    async def set_report_channel_command(interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        
        channel_id = channel.id
        
        if db is None:
            await interaction.followup.send("❌ データベースが接続されていません。デプロイを確認してください。", ephemeral=True)
            return

        if await bot._save_config(channel_id):
            
            if bot.daily_report.is_running():
                bot.daily_report.stop()
                await asyncio.sleep(1) 
            
            bot.daily_report.start()
            
            await interaction.followup.send(f"✅ レポート送信先が **{channel.mention}** に設定されました。\n毎日 JST 0:00 に全メンバーのレポートを送信します。", ephemeral=True)
        else:
            await interaction.followup.send("❌ 設定の保存に失敗しました。", ephemeral=True)

    @bot.tree.command(name="mytime", description="指定した期間の活動時間レポートを表示します。", guild=guild_id_object)
    @app_commands.choices(period=[
        app_commands.Choice(name="1日 (昨日)", value=1),
        app_commands.Choice(name="3日間", value=3)
    ])
    @app_commands.describe(period='集計する期間', member='活動時間を知りたいサーバーメンバー (省略可能)')
    async def mytime_command(interaction: discord.Interaction, period: app_commands.Choice[int], member: discord.Member = None):
        await interaction.response.defer()
        
        if db is None:
            await interaction.followup.send("❌ データベースが接続されていません。デプロイを確認してください。")
            return
            
        target_member = member if member is not None else interaction.user

        days = period.value 
        
        user_data = await get_user_report_data(target_member, db, bot.collection_path, days=days)
        
        await send_user_report_embed(interaction, target_member, user_data, days)
    
    @bot.tree.command(name="send_report_test", description="設定されたチャンネルへテストレポートを送信します。", guild=guild_id_object)
    async def send_report_test_command(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if db is None:
            await interaction.followup.send("❌ データベースが接続されていません。デプロイを確認してください。", ephemeral=True)
            return

        if bot.report_channel_id is None:
            # config_doc_refが未設定の場合、ここでロードを試みる
            await bot._load_config()

        channel_id = bot.report_channel_id 
        
        if channel_id is None:
            await interaction.followup.send("⚠️ レポート送信先チャンネルIDが設定されていません。\n`/set_report_channel` コマンドで設定してください。", ephemeral=True)
            return

        try:
            channel = bot.get_channel(channel_id) 
            if channel:
                test_embed = discord.Embed(
                    title="📝 テストレポート",
                    description="これは設定されたチャンネルへのテスト送信です。\n✅ 自動レポートは**毎日 JST 0:00** に送信されます。",
                    color=discord.Color.green()
                )
                await channel.send(embed=test_embed)
                await interaction.followup.send(f"✅ テストレポートをチャンネル: {channel.mention} に送信しました。", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ チャンネルID `{channel_id}` が見つからないか、Botにアクセス権限がありません。", ephemeral=True)
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
