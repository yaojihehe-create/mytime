import discord
import os
import json
import tempfile
import base64
from discord import app_commands
from discord.ext import commands, tasks
from flask import Flask
from threading import Thread # スレッド処理のために必須
from multiprocessing import current_process
from datetime import datetime, timedelta, timezone, time 
import asyncio
import logging 

# ログ設定: BotのカスタムメッセージとDiscordの詳細な接続情報を表示できるように設定
# レベルをINFOからDEBUGに引き上げ、より詳細な情報（Bot内部の処理やDiscord通信）を表示
logging.basicConfig(
    level=logging.DEBUG, # デバッグレベルに変更
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)
# Discordライブラリ自体のロガーをデバッグレベルに設定
discord_logger = logging.getLogger('discord')
discord_logger.setLevel(logging.DEBUG) 


# Firebase/Firestore関連のインポート
try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    logging.warning("警告: 'firebase-admin'ライブラリが見つかりません。Botを実行するにはインストールが必要です。")


# Flaskのアプリケーションインスタンスを作成（Webサーバーとして機能）
app = Flask(__name__)

# Firestore接続とBotの状態管理のためのグローバル変数
db = None
# 直前のユーザーのステータスと、その状態に移行した時刻 (ユーザーID -> (ステータスキー, datetimeオブジェクト))
last_status_updates = {} 
tz_jst = timezone(timedelta(hours=9)) # 日本時間 (JST)
# Botスレッドの状態管理用グローバル変数
bot_thread = None
# Botの準備完了状態を示すフラグ (Discordへの接続が完了したか)
bot_ready_status = False # 新しいグローバル変数

# Renderのヘルスチェック用ルートを追加 (Webサーバーの安定稼働のために必須)
@app.route('/')
def health_check():
    """Renderのヘルスチェックに応答するためのルート。"""
    global bot_thread, bot_ready_status # bot_ready_statusを追加
    
    status = "Bot is starting or failed."
    
    # Botスレッドが生きていれば
    if bot_thread and bot_thread.is_alive():
        # Discordへの接続が完了していれば
        if bot_ready_status:
            status = "Bot is running and ready."
        # スレッドは生きているが、まだ接続完了前であれば
        else:
            status = "Bot is connecting..."
            
    # Botの状態を正確に反映
    logging.info(f"Health Check: {status} (Process: {current_process().name})")
    return f"Status Check: {status}", 200

# Botクライアントの定義
class StatusTrackerBot(commands.Bot):
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.app_id = os.getenv("__app_id", "default-app-id")
        # ユーザーのステータスデータを保存するコレクションパス
        self.collection_path = f'artifacts/{self.app_id}/public/data/user_status'
        self.config_doc_ref = None
        self.report_channel_id = None # レポートチャンネルIDはサーバーIDではなく、チャンネルIDとして保存

    async def _initialize_db_references(self):
        """dbが初期化された後、ドキュメント参照を設定する"""
        global db
        if db is not None and self.config_doc_ref is None:
            logging.debug("Firestore Document References initialization.")
            # Botの設定（レポートチャンネルIDなど）を保存する場所
            self.config_doc_ref = db.collection(f'artifacts/{self.app_id}/public/data/bot_config').document('settings')
            return True
        return False

    async def _load_config(self):
        """FirestoreからレポートチャンネルIDをロードする"""
        if not await self._initialize_db_references():
            logging.warning("Database reference not ready when trying to load config.")
            return False

        try:
            # blocking I/O (Firestore get)をasyncio.to_threadで非同期に実行
            doc = await asyncio.to_thread(self.config_doc_ref.get)
            if doc.exists and 'report_channel_id' in doc.to_dict():
                self.report_channel_id = doc.to_dict()['report_channel_id']
                logging.info(f"✅ FirestoreからレポートチャンネルIDをロード: {self.report_channel_id}")
                return True
            else:
                logging.info("ℹ️ FirestoreにレポートチャンネルIDが見つかりませんでした。")
                return False
        except Exception as e:
            logging.error(f"❌ 設定ロード中にエラーが発生しました: {e}", exc_info=True)
            return False

    async def _save_config(self, channel_id: int):
        """FirestoreにレポートチャンネルIDを保存する"""
        if not await self._initialize_db_references():
            logging.error("❌ エラー: データベース参照が未設定のため、設定を保存できません。")
            return False

        try:
            logging.debug(f"Attempting to save report channel ID: {channel_id}")
            # blocking I/O (Firestore set)をasyncio.to_threadで非同期に実行
            await asyncio.to_thread(self.config_doc_ref.set, 
                                    {'report_channel_id': channel_id}, 
                                    merge=True)
            self.report_channel_id = channel_id
            logging.info(f"✅ レポートチャンネルIDをFirestoreに保存しました: {channel_id}")
            return True
        except Exception as e:
            logging.error(f"❌ 設定保存中にエラーが発生しました: {e}", exc_info=True)
            return False

    async def on_ready(self):
        global bot_ready_status # グローバルフラグにアクセス

        # 起動成功の確実なログ
        logging.info('---------------------------------')
        logging.info(f'✅ Botがログインしました: {self.user.name} (ID: {self.user.id})')
        logging.info(f'🔗 参加サーバー数: {len(self.guilds)}')
        for guild in self.guilds:
            logging.info(f'   - {guild.name} (ID: {guild.id})')
        
        # Botの準備完了フラグをTrueに設定
        bot_ready_status = True 
        
        # 1. データベース設定のロード
        await self._load_config()

        # 2. コマンドの強制同期 (グローバルコマンドとして同期)
        try:
            logging.info("--- 🔄 グローバルへの強制同期処理開始 ---")
            await self.tree.sync() 
            logging.info("--- ✅ グローバルへのコマンド同期完了 ---")

        except Exception as e:
            logging.warning(f"⚠️ 警告: スラッシュコマンド同期中のエラー: {e}")
            
        # 3. 記録漏れを防ぐための初期ステータス記録
        now = datetime.now(tz_jst)
        logging.info("--- 📊 ユーザーの初期ステータスを取得しています ---")
        
        for guild in self.guilds:
            try:
                logging.debug(f"Fetching members for Guild: {guild.name} ({guild.id})")
                await guild.chunk() # メンバーキャッシュを強制的に取得
                
                member_count = 0
                for member in guild.members:
                    if member.bot:
                        continue
                    
                    member_count += 1
                    user_id = member.id
                    
                    # 既に記録があるユーザーはスキップ（再起動時の重複記録を防ぐ）
                    if user_id in last_status_updates:
                        continue
                    
                    status_key = str(member.status)
                    
                    # 'invisible' は 'offline' として扱う
                    if status_key == 'invisible':
                        status_key = 'offline'
                        
                    last_status_updates[user_id] = (status_key, now)
                    
                logging.debug(f"Recorded initial status for {member_count} members in {guild.name}")
            except discord.Forbidden:
                logging.warning(f"⚠️ 警告: サーバー '{guild.name}' ({guild.id}) でメンバー情報の読み取りが拒否されました。PRESENCE INTENTとSERVER MEMBERS INTENTを確認してください。")
            except Exception as e:
                logging.error(f"❌ 初期ステータス記録中にエラーが発生しました ({guild.name}): {e}", exc_info=True)

        logging.info("✅ 初期ステータス記録完了。")

        # 4. 定期タスクの開始
        if self.report_channel_id is not None:
            if not self.daily_report.is_running():
                self.daily_report.start()
                logging.info(f"✅ 日次レポートタスクを開始しました。送信先: {self.report_channel_id}")
        else:
            logging.info("ℹ️ レポートチャンネルIDが未設定のため、自動送信をスキップします。/set_report_channelで設定してください。")
            
        logging.info('---------------------------------')
        
    async def on_interaction(self, interaction: discord.Interaction):
        """すべてのインタラクション（特にスラッシュコマンド）をログに記録する"""
        if interaction.type == discord.InteractionType.application_command:
            logging.info(f"--- 🚀 Command Executed ---")
            logging.info(f"User: {interaction.user.display_name} ({interaction.user.id})")
            logging.info(f"Command: /{interaction.command.name}")
            if interaction.guild:
                logging.info(f"Guild: {interaction.guild.name} ({interaction.guild.id})")
            if interaction.channel:
                logging.info(f"Channel: #{interaction.channel.name} ({interaction.channel.id})")
            logging.info(f"--------------------------")

        # 既存のイベント処理に渡す
        # commands.Botを継承しているため、super().on_interaction()を呼び出し、コマンド処理を続行
        await super().on_interaction(interaction)


    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        # Bot自身、またはデータベースが未接続の場合はスキップ
        if after.id == self.user.id or db is None:
            return

        user_id = after.id
        doc_ref = db.collection(self.collection_path).document(str(user_id))
        now = datetime.now(tz_jst)
        
        # ログ記録のための情報取得
        guild_info = f"Guild: {after.guild.name} ({after.guild.id})"
        user_info = f"{after.display_name} ({after.id})"
        log_time = now.strftime("%Y-%m-%d %H:%M:%S JST")
        
        # ------------------------------------------------------------------
        # 1. プレゼンスの変更ログ (ステータス、アクティビティ、ニックネーム)
        # ------------------------------------------------------------------
        
        # ステータス変更のログ
        if before.status != after.status:
            logging.info(f"[{log_time}] [STATUS CHANGE] {user_info} {guild_info} | Status: {before.status} -> {after.status}")
        
        # アクティビティ変更のログ
        if before.activities != after.activities:
            # ログ出力のためにアクティビティを整形
            before_activities = ", ".join([format_activity(a) for a in before.activities]) if before.activities else "None"
            after_activities = ", ".join([format_activity(a) for a in after.activities]) if after.activities else "None"
            logging.info(f"[{log_time}] [ACTIVITY CHANGE] {user_info} {guild_info} | Activities: Before='{before_activities}' | After='{after_activities}'")

        # ニックネーム変更のログ
        if before.display_name != after.display_name:
            logging.info(f"[{log_time}] [NICK CHANGE] {user_info} {guild_info} | Nickname: '{before.display_name}' -> '{after.display_name}'")
            
        # ------------------------------------------------------------------
        # 2. ステータス時間記録処理
        # ------------------------------------------------------------------
        
        # 時間記録のために、'invisible' は 'offline' として扱う
        current_status_key = str(after.status)
        if current_status_key == 'invisible':
            current_status_key = 'offline'

        # 起動時の初期記録があるか確認
        if user_id in last_status_updates:
            prev_status_key, prev_time = last_status_updates[user_id]
        else:
            # last_status_updatesにないがon_presence_updateが呼ばれた場合 (例外的な処理、通常はon_readyで設定される)
            logging.warning(f"[{log_time}] ⚠️ Missing initial status for {user_info}. Assuming 'offline' start time is 'now'.")
            prev_status_key = str(before.status) if before.status else 'offline'
            if prev_status_key == 'invisible':
                prev_status_key = 'offline'
            prev_time = now

        # ステータスが変わっていない場合は時間記録処理を終了
        if current_status_key == prev_status_key:
            return
            
        duration = (now - prev_time).total_seconds()
        
        # 記録する前のステータス名
        field_name = f'{prev_status_key}_seconds'
        
        # 状態変更が日をまたいだ場合を考慮し、記録は「前のステータスが続いていた日」の日付を使用
        prev_date_str = prev_time.strftime("%Y-%m-%d")
        date_field_name = f'{prev_date_str}_{field_name}'

        if duration > 0:
            logging.debug(f"[{log_time}] Recording time for {user_id}. Duration: {duration:.2f}s for {prev_status_key} on {prev_date_str}.")
            # FirestoreのIncrement機能を利用して、安全に時間を加算
            try:
                await asyncio.to_thread(doc_ref.set, {
                    field_name: firestore.Increment(duration), # ステータスごとの合計時間
                    date_field_name: firestore.Increment(duration), # 日付+ステータスごとの合計時間
                    'last_updated': now
                }, merge=True) 
                logging.debug(f"✅ Firestore updated successfully for {user_id}.")
            except Exception as e:
                logging.error(f"❌ Firestoreへのステータス時間記録中にエラーが発生しました: {e}", exc_info=True)

        # 最後の更新時刻を新しいステータスと時刻で更新
        last_status_updates[user_id] = (current_status_key, now)
        
    async def on_member_join(self, member):
        # Bot自身または他のBotはスキップ
        if member.bot:
            return

        now = datetime.now(tz_jst)
        status_key = str(member.status)
        
        # ログ出力: メンバー参加と初期ステータス
        log_time = now.strftime("%Y-%m-%d %H:%M:%S JST")
        logging.info(f"[{log_time}] 🆕 Member Joined! Guild: {member.guild.name} ({member.guild.id}), User: {member.display_name} ({member.id}), Initial Status: {status_key}")
        
        # last_status_updates に初期ステータスを登録
        if member.id not in last_status_updates:
             # 'invisible' は 'offline' として扱う
             if status_key == 'invisible':
                 status_key = 'offline'
             last_status_updates[member.id] = (status_key, now)
             logging.debug(f"Initial status set for new member {member.id}.")
        else:
             logging.debug(f"Member {member.id} already exists in last_status_updates (should not happen on join).")

    async def on_member_remove(self, member):
        # Bot自身または他のBotはスキップ
        if member.bot:
            return
            
        now = datetime.now(tz_jst)
        log_time = now.strftime("%Y-%m-%d %H:%M:%S JST")
        logging.info(f"[{log_time}] 🚪 Member Left! Guild: {member.guild.name} ({member.guild.id}), User: {member.display_name} ({member.id})")

        # last_status_updates から削除（メモリ解放のため）
        if member.id in last_status_updates:
            del last_status_updates[member.id]
            logging.debug(f"Member {member.id} removed from last_status_updates.")
        
    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """スラッシュコマンド実行中に発生したエラーを処理する"""
        
        logging.error(f"--- ❌ Command Error ---")
        logging.error(f"User: {interaction.user.display_name} ({interaction.user.id})")
        logging.error(f"Command: /{interaction.command.name}")
        logging.error(f"Error Type: {error.__class__.__name__}")
        logging.error(f"Error Details: {error}", exc_info=True)
        logging.error(f"--------------------------")

        # コマンドが見つからないエラーは無視
        if isinstance(error, app_commands.CommandNotFound):
            return

        # 権限エラーの場合
        if isinstance(error, app_commands.MissingPermissions) or isinstance(error, app_commands.MissingRole):
            try:
                await interaction.response.send_message(
                    "❌ **権限がありません**。\nこのコマンドを実行するには、必要なサーバー権限（例: チャンネル管理）が必要です。", 
                    ephemeral=True
                )
            except discord.InteractionResponded:
                 await interaction.followup.send(
                    "❌ **権限がありません**。\nこのコマンドを実行するには、必要なサーバー権限（例: チャンネル管理）が必要です。", 
                    ephemeral=True
                )
            return

        # その他のエラー
        error_message = f"❌ コマンド実行中にエラーが発生しました。\n`{error.__class__.__name__}: {error}`\n詳細はBotのログを確認してください。"
        
        if interaction.response.is_done():
            await interaction.followup.send(error_message, ephemeral=True)
        else:
            try:
                await interaction.response.send_message(error_message, ephemeral=True)
            except discord.HTTPException as e:
                logging.error(f"応答送信時の HTTP エラー: {e}")
            
    # ----------------------------------------------------
    # 日次レポートタスク (毎日 JST 00:00 実行)
    # ----------------------------------------------------
    @tasks.loop(time=time(0, 0, tzinfo=tz_jst)) 
    async def daily_report(self):
        logging.info("--- 🔄 日次レポートタスク開始前の設定再ロード ---")
        # タスク実行前に設定を再ロード
        await self._load_config() 
        
        if not self.is_ready() or db is None or self.report_channel_id is None:
            logging.warning("⚠️ 警告: レポートタスクの実行条件が満たされていません。タスクをスキップします。")
            return

        report_channel = self.get_channel(self.report_channel_id)
        if not report_channel:
            logging.warning(f"⚠️ 警告: レポートチャンネルID {self.report_channel_id} が無効です。")
            return

        # チャンネルが属するサーバーIDを取得
        target_guild = report_channel.guild
        
        logging.info(f"--- 📅 日次レポート処理開始 ({target_guild.name} / ID: {target_guild.id}, JST 00:00) ---")

        days = 1 # 昨日1日間のレポート
        
        member_reports_sent = 0
        
        # 全メンバー（Bot以外）を対象にレポートを作成し、送信
        for member in target_guild.members:
            if member.bot:
                continue
            
            # ユーザーのデータを取得 (昨日1日分)
            user_data = await get_user_report_data(member, db, self.collection_path, days=days)
            
            # データが存在しないか、合計時間が0の場合はスキップ
            if not user_data or user_data.get('total', 0) == 0:
                logging.debug(f"No activity found for daily report: {member.display_name} ({member.id})")
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
                member_reports_sent += 1
                await asyncio.sleep(0.5) # レートリミット回避のための一時停止
            except Exception as e:
                logging.error(f"❌ レポート送信失敗 (ユーザーID: {member.id}): {e}")

        logging.info(f"--- ✅ 日次レポート処理完了。送信数: {member_reports_sent} ---")
        
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
    if status == 'invisible': return '⚫ オフライン' # ステルス表示を削除
    return status.capitalize()

def format_activity(activity: discord.Activity) -> str:
    """Discordのアクティビティ情報を整形する"""
    if activity.type == discord.ActivityType.playing:
        return f"Playing: {activity.name}"
    elif activity.type == discord.ActivityType.streaming:
        return f"Streaming: {activity.name} (URL: {activity.url})"
    elif activity.type == discord.ActivityType.listening:
        return f"Listening: {activity.name}"
    elif activity.type == discord.ActivityType.watching:
        return f"Watching: {activity.name}"
    elif activity.type == discord.ActivityType.custom:
        return f"Custom Status: {activity.name or 'N/A'}"
    else:
        return f"{activity.type.name.capitalize()}: {activity.name}"

async def get_user_report_data(member: discord.Member, db, collection_path, days=7):
    """Firestoreから指定した日数分の活動データを取得し集計する"""
    doc_ref = db.collection(collection_path).document(str(member.id))
    
    try:
        # blocking I/O (Firestore get)をasyncio.to_threadで非同期に実行
        doc = await asyncio.to_thread(doc_ref.get)
    except Exception as e:
        # Firestoreアクセスエラーを捕捉
        logging.error(f"❌ Firestoreからのデータ取得中にエラーが発生しました (ユーザーID: {member.id}): {e}", exc_info=True)
        return None

    if not doc.exists:
        logging.debug(f"No document found for user {member.id}.")
        return None

    data = doc.to_dict()
    now = datetime.now(tz_jst)
    # NOTE: 'invisible' は on_presence_update で 'offline' として記録されるため、集計は 'offline' に一本化される
    statuses = ['online', 'idle', 'dnd', 'offline'] 
    
    total_sec = 0
    online_sec = 0
    offline_sec = 0
    user_data = {}

    for status in statuses:
        status_total_sec = 0
        for i in range(days):
            # i=0が当日、i=1が昨日... となるため、days=1の場合は昨日分のみ集計
            # レポート対象は「現在時刻の days 日前」まで
            target_date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            field = f'{target_date}_{status}_seconds'
            status_total_sec += data.get(field, 0)
        
        user_data[status] = status_total_sec
        total_sec += status_total_sec
        
        if status in ['online', 'idle', 'dnd']:
            online_sec += status_total_sec
        elif status == 'offline':
            offline_sec += status_total_sec # invisibleもofflineとして記録されている
            
    # ユーザーが現在オンラインの場合、現在のステータスを一時的に加算して「現在までの合計」を表示する
    # ここでは、レポートの対象期間（過去 days 日間）に記録された時間のみを返します。
    user_data['total'] = total_sec
    user_data['online_time_s'] = online_sec
    user_data['offline_time_s'] = offline_sec
    
    logging.debug(f"Report data fetched for {member.id}: Total={total_sec:.2f}s")
    
    return user_data

async def send_user_report_embed(interaction: discord.Interaction, member: discord.Member, user_data: dict, days: int):
    """活動レポートをEmbedメッセージとして送信する"""
    
    # ユーザーデータが存在しないか、合計時間が0の場合はエラーメッセージを返す
    if not user_data or user_data.get('total', 0) == 0:
        await interaction.followup.send(f"⚠️ **{member.display_name}** さんの過去 **{days}** 日間の活動記録は見つかりませんでした。", ephemeral=True)
        return

    online_time = user_data.get('online_time_s', 0)
    offline_time = user_data.get('offline_time_s', 0)
    total_sec = online_time + offline_time
    
    if total_sec == 0:
        await interaction.followup.send(f"⚠️ **{member.display_name}** さんの過去 **{days}** 日間の活動記録は見つかりませんでした。", ephemeral=True)
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
    
    statuses = ['online', 'idle', 'dnd', 'offline'] # 'invisible' は含めない
    status_field_value = []
    
    for status in statuses:
        sec = user_data.get(f'{status}', 0)
        if sec > 0:
            status_field_value.append(f"{get_status_emoji(status)}: {format_time(sec)}")
    
    if status_field_value:
        embed.add_field(
            name="詳細内訳",
            value="\n".join(status_field_value),
            inline=False
        )
    
    embed.set_footer(text=f"レポート生成時刻: {datetime.now(tz_jst).strftime('%Y/%m/%d %H:%M:%S JST')}")
    
    # follow upでレスポンスを送信
    await interaction.followup.send(embed=embed)


# -----------------
# スラッシュコマンド
# -----------------
bot = StatusTrackerBot(
    command_prefix='!', 
    # NOTE: Intentsの二重定義を修正。Intents.all()のみを残す。
    intents=discord.Intents.all() # Botに必要なインテントを有効化
)
bot.remove_command('help') # デフォルトのhelpコマンドを削除

# /report コマンドの定義
@bot.tree.command(name="report", description="過去N日間の活動時間レポートを表示します。")
@app_commands.describe(
    member="レポートを表示するメンバーを選択（省略可能）",
    days="集計する日数 (1〜30日)",
)
async def report_command(interaction: discord.Interaction, member: discord.Member = None, days: app_commands.Range[int, 1, 30] = 7):
    # すぐに応答できないため、Defer (応答待ち) 状態にする
    await interaction.response.defer(ephemeral=False, thinking=True)
    
    target_member = member if member is not None else interaction.user
    
    if db is None:
        await interaction.followup.send("❌ データベースがまだ初期化されていません。しばらく待ってから再度お試しください。", ephemeral=True)
        return

    # データ取得
    user_data = await get_user_report_data(target_member, db, bot.collection_path, days)
    
    # レポート埋め込みメッセージの送信
    await send_user_report_embed(interaction, target_member, user_data, days)


# /set_report_channel コマンドの定義 (管理者権限が必要)
@bot.tree.command(name="set_report_channel", description="日次レポートを送信するチャンネルを設定します。")
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.describe(
    channel="レポートを送信するテキストチャンネルを選択"
)
async def set_report_channel_command(interaction: discord.Interaction, channel: discord.TextChannel):
    # すぐに応答できないため、Defer (応答待ち) 状態にする
    await interaction.response.defer(ephemeral=True, thinking=True)

    if db is None:
        await interaction.followup.send("❌ データベースがまだ初期化されていません。", ephemeral=True)
        return

    # 設定保存
    success = await bot._save_config(channel.id)

    if success:
        # タスクがまだ開始されていない場合は開始
        if not bot.daily_report.is_running():
            bot.daily_report.start()
            logging.info("日次レポートタスクを開始しました (コマンド実行による起動)。")
        
        await interaction.followup.send(
            f"✅ 日次レポートの送信先を **{channel.mention}** に設定しました。\n毎日JST 00:00にレポートが送信されます。", 
            ephemeral=True
        )
    else:
        await interaction.followup.send("❌ 設定の保存に失敗しました。ログを確認してください。", ephemeral=True)

# -----------------
# WebサーバーとBotの実行
# -----------------
def run_bot():
    """Botのメインループを実行する関数 (別スレッドで実行される)"""
    
    # Discordトークンは環境変数から取得
    DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    
    if not DISCORD_BOT_TOKEN:
        logging.error("❌ DISCORD_BOT_TOKEN環境変数が設定されていません。Botは起動できません。")
        # Botの実行を中止
        return

    # Botを起動
    try:
        # Note: run()はブロッキング関数であり、Botが切断されるまで戻らない
        logging.info("Botクライアントを起動しています...")
        logging.info(f"🔑 Discordに接続を試行しています... (Process ID: {os.getpid()})")
        bot.run(DISCORD_BOT_TOKEN) 
    except Exception as e:
        # Bot実行中の致命的なエラーを捕捉
        logging.error(f"❌ Bot実行中に致命的なエラーが発生しました: {e}", exc_info=True)
    
    # Bot.run()が予期せず終了した場合（ホスティング環境による強制終了や未捕捉の致命的エラーなど）
    logging.critical("🛑 Botのメインループが予期せず終了しました。ホスティング環境による強制終了または未捕捉の致命的なエラーが原因の可能性があります。")


def init_firestore():
    """Firestoreの初期化を行う関数"""
    global db
    
    # Firestore設定を環境変数からロード
    try:
        # Canvas環境から提供されるFirebase Configと初期認証トークンをロード
        firebase_config_str = os.getenv("__firebase_config")
        
        if not firebase_config_str:
            logging.error("❌ __firebase_config環境変数が設定されていません。Firestoreを使用できません。")
            return
            
        firebase_config = json.loads(firebase_config_str)
        logging.debug("✅ __firebase_config環境変数をロードし、JSONとして解析しました。")
        
        # Firebase Admin SDKの初期化
        # Admin SDKの資格情報を、GCP/Firebaseのプロジェクト設定に基づいて作成
        # ここでは、Admin SDKの認証情報が環境内で自動的に提供されていると仮定し、defaultを使用します
        
        if not firebase_admin._apps: # 既に初期化されていないかチェック
            logging.info("Attempting to initialize Firebase Admin SDK...")
            
            # サービスアカウントキーの環境変数 (例: FIREBASE_SERVICE_ACCOUNT_KEY) を使用して初期化を試みる
            firebase_admin.initialize_app(
                credentials.Certificate({
                    "type": "service_account",
                    "project_id": firebase_config.get("projectId", "default-project-id"),
                    # サービスアカウントキーのその他のフィールドもここに追加する必要があります
                }), 
                {'projectId': firebase_config['projectId']}
            )
            logging.info("✅ Firebase Admin SDK initialized successfully.")
        
        # Firestoreインスタンスを取得
        db = firestore.client()
        logging.info("✅ Firestore client obtained.")
        
    except Exception as e:
        logging.error(f"❌ Firestore初期化中に致命的なエラーが発生しました: {e}", exc_info=True)
        global bot_ready_status
        bot_ready_status = False # DB初期化失敗時はBotを非稼働状態にする
        db = None # DBインスタンスをNoneに設定

def start_bot_and_webserver():
    """BotとFlask Webサーバーをそれぞれ別のスレッドで起動する"""
    global bot_thread

    # 1. Firestoreの初期化をメインスレッドで行う
    init_firestore()

    # 2. Botの実行を別スレッドで開始
    logging.info("Bot実行スレッドを開始します...")
    bot_thread = Thread(target=run_bot, name="DiscordBotThread")
    bot_thread.daemon = True # メインプロセス終了時にスレッドも終了
    bot_thread.start()
    logging.info(f"Bot実行スレッド: {bot_thread.name} (Thread ID: {bot_thread.ident}) が起動しました。メインプロセス ID: {os.getpid()}")

    # 3. Flask Webサーバーの起動 (この関数を呼び出したプロセス/スレッドが担当)
    # 外部からのアクセスを許可するために host='0.0.0.0' を指定
    # Flaskはブロッキング関数であるため、この呼び出しがBot実行のメインループとなる
    logging.info("Flask Webサーバーを起動します (host=0.0.0.0, port=8080)...")
    try:
        app.run(host='0.0.0.0', port=os.environ.get('PORT', 8080))
    except Exception as e:
        logging.critical(f"❌ Flask Webサーバー起動中に致命的なエラーが発生しました: {e}", exc_info=True)

# スクリプトが直接実行された場合にBotとWebサーバーを起動
if __name__ == '__main__':
    start_bot_and_webserver()
