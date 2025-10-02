import discord
import os
import json
import random
import tempfile
import base64
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask
from threading import Thread
from multiprocessing import current_process
# datetimeモジュールからtimeクラスを明示的にインポート
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

# Botクライアントの定義
class StatusTrackerBot(commands.Bot):
    # チャンネルIDの初期値はNoneとし、Firestoreからロードする
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.app_id = os.getenv("__app_id", "default-app-id")
        self.collection_path = f'artifacts/{self.app_id}/public/data/user_status'
        # チャンネル設定を保存するコレクションパス
        self.config_doc_ref = db.collection(f'artifacts/{self.app_id}/public/data/bot_config').document('settings')
        self.report_channel_id = None # Bot起動時にFirestoreからロードされる

    async def _load_config(self):
        """FirestoreからレポートチャンネルIDをロードする"""
        try:
            # Firestoreの処理はIOバウンドなのでto_threadを使用
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
        try:
            # Firestoreの処理はIOバウンドなのでto_threadを使用
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
        print('Botはサーバーのユーザー活動時間を記録します。')
        
        # 1. 設定のロード
        await self._load_config()

        try:
            # 📌 修正点: コマンド重複解消のための**最も安定した**グローバル同期処理
            # on_readyでギルドをループすると、キャッシュ構築のタイミングによりNoneTypeエラーが発生し、
            # コマンド再登録が失敗することが判明したため、グローバルクリア＆再同期に絞り込む。
            print("--- コマンド重複解消のための最終同期処理開始 ---")
            
            # 1. グローバルコマンド定義をクリア
            await self.tree.clear_commands(guild=None) 
            
            # 2. グローバル同期を行い、削除を適用し、Botに定義されている新しいコマンドを登録し直す
            await self.tree.sync()
            
            print("--- コマンド同期完了 (NoneTypeエラー回避)。Discordクライアントの再起動が必要な場合があります。 ---")

        except Exception as e:
            # NoneTypeエラー回避のため、Botの起動自体は止めないようにログ出力のみ行う
            print(f"スラッシュコマンド同期中のエラー: {e}")
            
        # 2. 記録漏れを防ぐための初期ステータス記録
        now = datetime.now(tz_jst)
        print("ユーザーの初期ステータスを取得しています...")
        for guild in self.guilds:
            # メンバーキャッシュがまだ構築中の可能性があるため、念のため待機
            await guild.chunk() 
            for member in guild.members:
                if member.bot or member.id in last_status_updates:
                    continue
                
                # Bot起動時の現在のステータスを記録し、次のステータス変更に備える
                status_key = str(member.status)
                last_status_updates[member.id] = (status_key, now)
        print("初期ステータス記録完了。")

        # 3. ロードされたIDに基づいてタスクを開始
        if self.report_channel_id is not None:
            self.daily_report.start()
            print(f"日次レポートタスクを開始しました。送信先: {self.report_channel_id}")
        else:
            print("レポートチャンネルIDが未設定のため、自動送信をスキップします。/set_report_channelで設定してください。")
            
        print('---------------------------------')

    async def on_guild_join(self, guild: discord.Guild):
        """新しいサーバーに参加した際、即座にスラッシュコマンドを同期する"""
        try:
            print(f"新しいサーバーに参加しました: {guild.name} ({guild.id})。コマンドを同期します...")
            # 新規サーバーでは重複がないため、コピー＆同期でOK
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"サーバー {guild.name} へのスラッシュコマンド同期が完了しました。")
        except Exception as e:
            print(f"新しいサーバーへのスラッシュコマンド同期エラー: {e}")

    async def on_presence_update(self, before, after):
        # Bot自身、またはデータベースが未接続の場合はスキップ
        if after.id == self.user.id or db is None:
            return

        user_id = after.id
        doc_ref = db.collection(self.collection_path).document(str(user_id))
        now = datetime.now(tz_jst)
        current_status_key = str(after.status)

        # 記録の正確性を向上させるためのロジック
        if user_id in last_status_updates:
            prev_status_key, prev_time = last_status_updates[user_id]
        else:
            # 記録がない場合、before.statusを初期ステータスとして扱う
            prev_status_key = str(before.status) if before.status else 'offline'
            prev_time = now

        # ステータスが変わっていない場合は処理を終了
        if current_status_key == prev_status_key:
            return

        # 経過時間を計算
        duration = (now - prev_time).total_seconds()
        
        # 経過時間フィールド名（例: online_seconds）
        field_name = f'{prev_status_key}_seconds'
        
        # 日付付き経過時間フィールド名（例: 2025-10-02_online_seconds）
        date_field_name = f'{prev_time.strftime("%Y-%m-%d")}_{field_name}'

        # 経過時間が0より大きい場合のみFirestoreに書き込み
        if duration > 0:
            # Firestoreへの書き込みを非同期 (to_thread) で実行し、Botの反応停止を防ぐ
            # merge=Trueなのでデータが消える心配はありません。
            await asyncio.to_thread(doc_ref.set, {
                # 累計時間に加算
                field_name: firestore.Increment(duration),
                # 日付別時間に加算
                date_field_name: firestore.Increment(duration),
                # 最終更新時刻を記録
                'last_updated': now
            }, merge=True) 

        # 最後に、現在のステータスと時刻を更新
        last_status_updates[user_id] = (current_status_key, now)
        
    # ----------------------------------------------------
    # 📌 日次レポートタスク (毎日 JST 00:00 実行)
    # ----------------------------------------------------
    # time=time(...) に修正
    @tasks.loop(time=time(0, 0, tzinfo=tz_jst)) 
    async def daily_report(self):
        # 毎回の実行前に最新のIDをロード (念のため)
        await self._load_config() 
        
        if not self.is_ready() or db is None or self.report_channel_id is None:
            print("自動レポートがスキップされました。Botの準備ができていないか、チャンネルIDが未設定です。")
            return

        report_channel = self.get_channel(self.report_channel_id)
        if not report_channel:
            print(f"チャンネルID {self.report_channel_id} が見つからないか、アクセスできません。")
            return

        print("--- 日次レポート処理開始 (JST 00:00) ---")

        for guild in self.guilds:
            days = 1 # 前日分のレポート
            
            # 全メンバーのレポートを生成し、チャンネルに送信
            for member in guild.members:
                if member.bot:
                    continue
                
                user_data = await get_user_report_data(member, db, self.collection_path, days=days)
                
                # 活動記録が見つからなかった、または合計時間が0の場合はスキップ
                if not user_data or user_data.get('total', 0) == 0:
                    continue

                # レポートEmbedの作成 (send_user_report_embedから流用)
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

                await report_channel.send(embed=embed)
                await asyncio.sleep(0.5) # APIレート制限対策
        
        print("--- 日次レポート処理完了 ---")
        
    @daily_report.before_loop
    async def before_daily_report(self):
        await self.wait_until_ready()


# -----------------
# レポート表示用のヘルパー関数 (変更なし)
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
    # Firestoreのget()は同期処理のため、to_threadを使用
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
            # 昨日からさかのぼって日付を計算
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
# Firestore初期化関数 (変更なし)
# -----------------
def init_firestore():
    global db
    if db is not None:
        return db

    base64_config = os.getenv("__firebase_config")
    
    if not base64_config:
        print("致命的エラー: __firebase_config 環境変数が設定されていません。")
        return None

    temp_file_path = None
    try:
        json_bytes = base64.b64decode(base64_config)
        json_str = json_bytes.decode('utf-8')
        
        with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8') as temp_file:
            temp_file.write(json_str)
            temp_file_path = temp_file.name

        cred = credentials.Certificate(temp_file_path)
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
        print("Botはデータベース接続なしで起動できません。")
        return

    TOKEN = os.getenv("DISCORD_TOKEN")
    
    intents = discord.Intents.default()
    intents.members = True
    intents.presences = True
    intents.message_content = True

    # チャンネルIDのハードコードを削除し、Botインスタンスを生成
    bot = StatusTrackerBot(command_prefix='!', intents=intents)

    @bot.tree.command(name="set_report_channel", description="日次レポートの送信先チャンネルを設定します。")
    @app_commands.describe(channel='レポートを送信するテキストチャンネル')
    async def set_report_channel_command(interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        
        channel_id = channel.id
        
        # FirestoreにチャンネルIDを保存
        if await bot._save_config(channel_id):
            
            # 定期実行タスクが既に実行されているかチェックし、停止・再開
            if bot.daily_report.is_running():
                bot.daily_report.stop()
                await asyncio.sleep(1) # タスク停止を待機
            
            bot.daily_report.start()
            
            await interaction.followup.send(f"✅ レポート送信先が **{channel.mention}** に設定されました。\n毎日 JST 0:00 に全メンバーのレポートを送信します。", ephemeral=True)
        else:
            await interaction.followup.send("❌ 設定の保存に失敗しました。", ephemeral=True)

    @bot.tree.command(name="mytime", description="指定した期間の活動時間レポートを表示します。")
    @app_commands.choices(period=[
        app_commands.Choice(name="1日 (昨日)", value=1),
        app_commands.Choice(name="3日間", value=3)
    ])
    @app_commands.describe(period='集計する期間', member='活動時間を知りたいサーバーメンバー (省略可能)')
    async def mytime_command(interaction: discord.Interaction, period: app_commands.Choice[int], member: discord.Member = None):
        await interaction.response.defer()
        
        # メンバーが指定されなかった場合はコマンド実行者自身を対象とする
        target_member = member if member is not None else interaction.user

        days = period.value # 選択された期間 (1 または 3)
        
        user_data = await get_user_report_data(target_member, db, bot.collection_path, days=days)
        
        await send_user_report_embed(interaction, target_member, user_data, days)
    
    # チャンネルIDの使用例を示すテストコマンド
    @bot.tree.command(name="send_report_test", description="設定されたチャンネルへテストレポートを送信します。")
    async def send_report_test_command(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

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
