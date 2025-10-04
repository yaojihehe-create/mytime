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
# 注: このプロジェクトではfirebase_adminが使用できる環境が必要です。
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    print("警告: 'firebase-admin'ライブラリが見つかりません。Botを実行するにはインストールが必要です。")
    exit()


# Flaskのアプリケーションインスタンスを作成（Webサーバーとして機能）
app = Flask(__name__)

# Firestore接続とBotの状態管理のためのグローバル変数
db = None
# 直前のユーザーのステータスと、その状態に移行した時刻 (ステータスキー, datetimeオブジェクト)
last_status_updates = {} 
tz_jst = timezone(timedelta(hours=9)) # 日本時間 (JST)

# ====================================================
# 🚨 必須の修正点 1: ターゲットサーバーIDを設定 🚨
# ----------------------------------------------------
# !!! ここをあなたのBotを導入したいサーバーのIDに置き換えてください (数字のみ) !!!
# !!! サーバーIDが設定されていないと、Botは動作しません !!!
# 例: TARGET_GUILD_ID = 123456789012345678
TARGET_GUILD_ID = 1417850283131797506 
# ====================================================

if TARGET_GUILD_ID == 0:
    print("FATAL ERROR: TARGET_GUILD_IDが0のままです。正しいサーバーIDに設定してください。")
    # Bot起動を停止するため、後続の処理でエラーを発生させる

# Botクライアントの定義
class StatusTrackerBot(commands.Bot):
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.app_id = os.getenv("__app_id", "default-app-id")
        # ユーザーのステータスデータを保存するコレクションパス
        self.collection_path = f'artifacts/{self.app_id}/public/data/user_status'
        self.config_doc_ref = None
        self.report_channel_id = None
        # ターゲットギルドのオブジェクトを定義 (コマンド同期に必要)
        self.target_guild_object = discord.Object(id=TARGET_GUILD_ID)

    async def _initialize_db_references(self):
        """dbが初期化された後、ドキュメント参照を設定する"""
        global db
        if db is not None and self.config_doc_ref is None:
            # Botの設定（レポートチャンネルIDなど）を保存する場所
            self.config_doc_ref = db.collection(f'artifacts/{self.app_id}/public/data/bot_config').document('settings')
            return True
        return False

    async def _load_config(self):
        """FirestoreからレポートチャンネルIDをロードする"""
        if not await self._initialize_db_references():
            return False

        try:
            # blocking I/O (Firestore get)をasyncio.to_threadで非同期に実行
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
            # blocking I/O (Firestore set)をasyncio.to_threadで非同期に実行
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
            await self.tree.sync(guild=self.target_guild_object)
            print(f"--- ターゲットサーバーへのコマンド同期完了 ---")

        except Exception as e:
            print(f"スラッシュコマンド同期中のエラー: {e}")
            
        # 3. 記録漏れを防ぐための初期ステータス記録
        now = datetime.now(tz_jst)
        print("ユーザーの初期ステータスを取得しています...")
        target_guild = self.get_guild(TARGET_GUILD_ID)
        
        if target_guild:
            await target_guild.chunk() # メンバーキャッシュを強制的に取得
            for member in target_guild.members:
                # Botはスキップ、既に記録があるユーザーもスキップ
                if member.bot or member.id in last_status_updates:
                    continue
                
                status_key = str(member.status)
                last_status_updates[member.id] = (status_key, now)
        else:
             print(f"警告: ターゲットサーバーID {TARGET_GUILD_ID} のサーバーが見つかりません。Botがそのサーバーに参加しているか確認してください。")

        print("初期ステータス記録完了。")

        # 4. 定期タスクの開始
        if self.report_channel_id is not None:
            # 初回起動時は、タスクが設定時刻まで実行されないようにする
            if not self.daily_report.is_running():
                self.daily_report.start()
                print(f"日次レポートタスクを開始しました。送信先: {self.report_channel_id}")
        else:
            print("レポートチャンネルIDが未設定のため、自動送信をスキップします。/set_report_channelで設定してください。")
            
        print('---------------------------------')

    async def on_presence_update(self, before, after):
        # Bot自身、またはデータベースが未接続の場合はスキップ
        if after.id == self.user.id or db is None:
            return

        # メンバーがターゲットサーバー内にいるか確認
        if after.guild.id != TARGET_GUILD_ID:
            return

        user_id = after.id
        doc_ref = db.collection(self.collection_path).document(str(user_id))
        now = datetime.now(tz_jst)
        current_status_key = str(after.status)

        # 起動時の初期記録があるか確認し、なければ前の状態を使用
        if user_id in last_status_updates:
            prev_status_key, prev_time = last_status_updates[user_id]
        else:
            # last_status_updatesにないが、on_presence_updateが呼ばれた場合 (Bot起動前に状態変更があった可能性)
            prev_status_key = str(before.status) if before.status else 'offline'
            prev_time = now # この場合、durationは0になるか、非常に短い時間になるため、大きな問題はない

        # ステータスが変わっていない場合は処理を終了
        if current_status_key == prev_status_key:
            return

        duration = (now - prev_time).total_seconds()
        field_name = f'{prev_status_key}_seconds'
        
        # 状態変更が日をまたいだ場合を考慮し、記録は「前のステータスが続いていた日」の日付を使用
        prev_date_str = prev_time.strftime("%Y-%m-%d")
        date_field_name = f'{prev_date_str}_{field_name}'

        if duration > 0:
            # FirestoreのIncrement機能を利用して、安全に時間を加算
            await asyncio.to_thread(doc_ref.set, {
                field_name: firestore.Increment(duration), # ステータスごとの合計時間
                date_field_name: firestore.Increment(duration), # 日付+ステータスごとの合計時間
                'last_updated': now
            }, merge=True) 

        # 最後の更新時刻を新しいステータスと時刻で更新
        last_status_updates[user_id] = (current_status_key, now)
        
    # ----------------------------------------------------
    # 日次レポートタスク (毎日 JST 00:00 実行)
    # ----------------------------------------------------
    @tasks.loop(time=time(0, 0, tzinfo=tz_jst)) 
    async def daily_report(self):
        # タスク実行前に設定を再ロード
        await self._load_config() 
        
        if not self.is_ready() or db is None or self.report_channel_id is None:
            print("警告: レポートタスクの実行条件が満たされていません。")
            return

        report_channel = self.get_channel(self.report_channel_id)
        if not report_channel:
            print(f"警告: レポートチャンネルID {self.report_channel_id} が無効です。")
            return

        print("--- 日次レポート処理開始 (JST 00:00) ---")

        # ターゲットギルドのみを処理
        target_guild = self.get_guild(TARGET_GUILD_ID)
        if target_guild:
            days = 1 # 昨日1日間のレポート
            
            # 全メンバー（Bot以外）を対象にレポートを作成し、送信
            for member in target_guild.members:
                if member.bot:
                    continue
                
                # ユーザーのデータを取得 (昨日1日分)
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
                    await asyncio.sleep(0.5) # レートリミット回避のための一時停止
                except Exception as e:
                    print(f"レポート送信失敗 (ユーザーID: {member.id}): {e}")

        print("--- 日次レポート処理完了 ---")
        
    @daily_report.before_loop
    async def before_daily_report(self):
        # Botの起動と接続が完了するまで待機
        await self.wait_until_ready()


# -----------------
# ヘルパー関数
# -----------------
def format_time(seconds: float) -> str:
    """秒数を「X時間 Y分 Z.zs」形式に整形する"""
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
    """ステータス名に対応する絵文字と名前を返す"""
    if status == 'online': return '🟢 オンライン'
    if status == 'idle': return '🌙 退席中'
    if status == 'dnd': return '🔴 取り込み中'
    if status == 'offline': return '⚫ オフライン'
    return status.capitalize()

async def get_user_report_data(member: discord.Member, db, collection_path, days=7):
    """Firestoreから指定した日数分の活動データを取得し集計する"""
    doc_ref = db.collection(collection_path).document(str(member.id))
    # blocking I/O (Firestore get)をasyncio.to_threadで非同期に実行
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
            # i=0が当日、i=1が昨日... となるため、days=1の場合は昨日分のみ集計
            date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            field = f'{date}_{status}_seconds'
            status_total_sec += data.get(field, 0)
        
        user_data[status] = status_total_sec
        total_sec += status_total_sec
        
        if status in ['online', 'idle', 'dnd']:
            online_sec += status_total_sec
        elif status == 'offline':
            offline_sec += status_total_sec

    # ユーザーが現在オンラインの場合、現在のステータスを一時的に加算して「現在までの合計」を表示する
    # ただし、レポートでは通常、前の日までの完了した時間のみを扱う
    # ここでは、レポートの対象期間（過去 days 日間）に記録された時間のみを返します。
    user_data['total'] = total_sec
    user_data['online_time_s'] = online_sec
    user_data['offline_time_s'] = offline_sec
    
    return user_data

async def send_user_report_embed(interaction: discord.Interaction, member: discord.Member, user_data: dict, days: int):
    """活動レポートをEmbedメッセージとして送信する"""
    
    # ユーザーデータが存在しないか、合計時間が0の場合はエラーメッセージを返す
    if not user_data or user_data.get('total', 0) == 0:
        await interaction.followup.send(f"⚠️ **{member.display_name}** さんの過去 {days} 日間の活動記録は見つかりませんでした。")
        return

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
        # 全体に対する割合を計算
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
# Firestore初期化関数
# -----------------
def init_firestore():
    global db
    if db is not None:
        return db

    # 必須の環境変数（Base64エンコードされたJSON文字列）を取得
    base64_config = os.getenv("__firebase_config")
    
    if not base64_config:
        print("致命的エラー: __firebase_config 環境変数が設定されていません。")
        return None 

    temp_file_path = None
    try:
        # Base64デコード
        json_bytes = base64.b64decode(base64_config)
        json_str = json_bytes.decode('utf-8')
        
        # 認証情報を一時ファイルとして保存（firebase-adminの要求）
        with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8') as temp_file:
            temp_file.write(json_str)
            temp_file_path = temp_file.name

        # 認証情報を使用してFirebaseアプリを初期化
        cred = credentials.Certificate(temp_file_path)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
            
        db = firestore.client()
        print("Firestore接続完了。")
        return db
        
    except Exception as e:
        print(f"Firestore初期化に失敗しました。認証情報（__firebase_config）を確認してください: {e}")
        return None
    
    finally:
        # 一時ファイルを削除
        if temp_file_path and os.path.exists(temp_file_path):
             os.remove(temp_file_path)


# -----------------
# Discord Bot本体の起動関数
# -----------------
def run_discord_bot():
    # gunicorn/Flask環境でBotがメインプロセスでのみ起動されるようにする
    if current_process().name != 'MainProcess':
        print(f"非メインプロセス ({current_process().name}) です。Botは起動しません。")
        return

    # Firestore接続を試みる
    if init_firestore() is None:
        print("Botの起動を停止します。Firestore接続エラーを確認してください。")
        return 

    TOKEN = os.getenv("DISCORD_TOKEN")
    
    if TARGET_GUILD_ID == 0:
        print("Botの起動を停止します。TARGET_GUILD_IDが設定されていません。")
        return

    # Botに必要な権限（インテント）を設定
    intents = discord.Intents.default()
    # ユーザーのステータスとアクティビティを追跡するために必須
    intents.members = True 
    intents.presences = True
    intents.message_content = True # スラッシュコマンドでは必須ではないが、一応含める

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

        # 設定を保存
        if await bot._save_config(channel_id):
            
            # 定期タスクを再起動して、新しいチャンネル設定を反映させる
            if bot.daily_report.is_running():
                bot.daily_report.stop()
                await asyncio.sleep(1) # 停止を待つ
                
            bot.daily_report.start()
            
            await interaction.followup.send(f"✅ レポート送信先が **{channel.mention}** に設定されました。\n毎日 JST 0:00 に全メンバーのレポートを送信します。", ephemeral=True)
        else:
            await interaction.followup.send("❌ 設定の保存に失敗しました。", ephemeral=True)

    @bot.tree.command(name="mytime", description="指定した期間の活動時間レポートを表示します。", guild=guild_id_object)
    @app_commands.choices(period=[
        app_commands.Choice(name="1日 (昨日)", value=1),
        app_commands.Choice(name="3日間", value=3)
        # より長い期間が必要ならここに追加
    ])
    @app_commands.describe(period='集計する期間', member='活動時間を知りたいサーバーメンバー (省略可能)')
    async def mytime_command(interaction: discord.Interaction, period: app_commands.Choice[int], member: discord.Member = None):
        await interaction.response.defer()
        
        if db is None:
            await interaction.followup.send("❌ データベースが接続されていません。デプロイを確認してください。")
            return
            
        target_member = member if member is not None else interaction.user

        days = period.value 
        
        # ユーザーデータを取得
        user_data = await get_user_report_data(target_member, db, bot.collection_path, days=days)
        
        # 結果をEmbedで送信
        await send_user_report_embed(interaction, target_member, user_data, days)
    
    @bot.tree.command(name="send_report_test", description="設定されたチャンネルへテストレポートを送信します。", guild=guild_id_object)
    async def send_report_test_command(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if db is None:
            await interaction.followup.send("❌ データベースが接続されていません。デプロイを確認してください。", ephemeral=True)
            return

        # config_doc_refが未設定の場合、ここでロードを試みる
        if bot.report_channel_id is None:
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
            # Botを実行
            bot.run(TOKEN)
        except Exception as e:
            print(f"Discord Bot 起動失敗: {e}")
    else:
        print("エラー: Botトークンが設定されていません。")

# -----------------
# Webサーバーのエンドポイント (Botを起動するためのエントリーポイント)
# -----------------
@app.route('/')
def home():
    if current_process().name == 'MainProcess':
        if not hasattr(app, 'bot_thread_started'):
            app.bot_thread_started = True
            print("Webアクセスを検知。Discord Botの起動を試みます...")
            
            # Botの実行はブロッキングなので、別スレッドで実行
            Thread(target=run_discord_bot).start()
            
            return "Discord Bot is initializing... (Please check Discord in 10 seconds)"
        else:
            return "Bot is alive!"
    else:
        return "Bot worker is alive (Sub-process)"
