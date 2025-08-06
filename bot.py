import discord
from discord.ext import commands, tasks
import os
from pymongo import MongoClient
from datetime import datetime, timedelta
import logging
import asyncio
from dotenv import load_dotenv
import uuid
import io
import aiohttp
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# Cấu hình logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Environment variables
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ROLE_NOTIFICATION_CHANNEL_ID = int(os.getenv("ROLE_NOTIFICATION_CHANNEL_ID", 0))
FILE_CATEGORY_ID = int(os.getenv("FILE_CATEGORY_ID", 0))  # ID danh mục cho kênh tạm thời
ROLE_DURATION_DAYS = int(os.getenv("ROLE_DURATION_DAYS", 50))
NOTIFICATION_THRESHOLD_DAYS = int(os.getenv("NOTIFICATION_THRESHOLD_DAYS", 5))
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "")  # ID thư mục Google Drive (tùy chọn)
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")

# Thiết lập bot Discord, vô hiệu hóa lệnh help mặc định
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="$", intents=intents, help_command=None)

# Thiết lập MongoDB
try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.server_info()
    db = mongo_client["discord_bot_db"]
    role_timers_collection = db["role_timers"]
    role_history_collection = db["role_history"]
    file_uploads_collection = db["file_uploads"]
    download_history_collection = db["download_history"]
    # Tạo index cho tìm kiếm nhanh
    file_uploads_collection.create_index([("file_name", "text")])
    download_history_collection.create_index([("download_id", 1)])
except Exception as e:
    logger.error(f"Không thể kết nối MongoDB: {e}")
    raise Exception(f"Không thể kết nối MongoDB: {e}")

# Thiết lập Google Drive API
SCOPES = ['https://www.googleapis.com/auth/drive']
creds = None
if os.path.exists('token.json'):
    creds = Credentials.from_authorized_user_file('token.json', SCOPES)
if not creds or not creds.valid:
    # Lấy nội dung credentials từ biến môi trường
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not credentials_json:
        raise Exception("Không tìm thấy GOOGLE_CREDENTIALS_JSON trong biến môi trường!")
    
    # Tạo file tạm thời từ nội dung JSON
    with open('temp_credentials.json', 'w') as temp_file:
        temp_file.write(credentials_json)
    
    flow = InstalledAppFlow.from_client_secrets_file('temp_credentials.json', SCOPES)
    creds = flow.run_local_server(port=0)
    with open('token.json', 'w') as token:
        token.write(creds.to_json())
    # Xóa file tạm thời
    os.remove('temp_credentials.json')
drive_service = build('drive', 'v3', credentials=creds)

# Ánh xạ role
role_mapping = {
    "giahan_new": "Gia hạn"  # Role chính cho $giahan
}
TIMED_ROLE_KEY = "giahan_new"
ADMIN_ROLES = ["Admin", "Mod", "Friendly Dev"]

# Hàm kiểm tra role
def has_role(member, role_names):
    """Kiểm tra xem thành viên có bất kỳ role nào trong danh sách role_names không."""
    return any(role.name in role_names for role in member.roles)

# Hàm định dạng thời gian còn lại
def format_remaining_time(expiration_time):
    """Định dạng thời gian còn lại thành chuỗi dễ đọc."""
    remaining = expiration_time - datetime.utcnow()
    total_seconds = remaining.total_seconds()
    if total_seconds <= 0:
        return "0 tháng 0 ngày 0 giờ 0 phút"
    days = int(total_seconds // (24 * 3600))
    months = days // 30
    days = days % 30
    total_seconds %= (24 * 3600)
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    return f"{months} tháng {days} ngày {hours} giờ {minutes} phút"

# Hàm upload file lên Google Drive
async def upload_to_drive(file_data, file_name):
    """Upload file lên Google Drive và trả về link chia sẻ."""
    try:
        file_metadata = {
            'name': file_name,
            'parents': [DRIVE_FOLDER_ID] if DRIVE_FOLDER_ID else []
        }
        media = MediaIoBaseUpload(io.BytesIO(file_data), mimetype='application/octet-stream')
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webViewLink'
        ).execute()
        drive_service.permissions().create(
            fileId=file['id'],
            body={'type': 'anyone', 'role': 'reader'}
        ).execute()
        return file['id'], file['webViewLink']
    except Exception as e:
        logger.error(f"Lỗi khi upload file lên Google Drive: {e}")
        if "insufficientParentPermissions" in str(e):
            logger.error(f"Bot không có quyền ghi vào thư mục {DRIVE_FOLDER_ID}. Kiểm tra quyền chia sẻ hoặc xóa DRIVE_FOLDER_ID trong .env.")
        return None, None

# Hàm tải file từ Google Drive
async def download_from_drive(file_id):
    """Tải file từ Google Drive và trả về dữ liệu file."""
    try:
        request = drive_service.files().get_media(fileId=file_id)
        file_data = io.BytesIO()
        downloader = MediaIoBaseDownload(file_data, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        file_data.seek(0)
        return file_data
    except Exception as e:
        logger.error(f"Lỗi khi tải file từ Google Drive: {e}")
        return None

# Hàm xóa role sau thời gian hết hạn
async def remove_role_after_delay(member, role, user_id, role_name):
    """Xóa role sau khi hết hạn và thông báo."""
    try:
        record = role_timers_collection.find_one({"user_id": user_id, "role_name": role_name})
        if record:
            duration = (record["expiration_time"] - datetime.utcnow()).total_seconds()
            if duration > 0:
                await asyncio.sleep(duration)
                try:
                    await member.remove_roles(role)
                    role_timers_collection.delete_one({"user_id": user_id, "role_name": role_name})
                    channel = bot.get_channel(ROLE_NOTIFICATION_CHANNEL_ID)
                    if channel:
                        await channel.send(f"{member.mention}, bạn đã hết giờ xem sếch, vui lòng liên hệ Admin!")
                except Exception as e:
                    logger.error(f"Lỗi khi gỡ role {role_name} cho user {user_id}: {e}")
    except Exception as e:
        logger.error(f"Lỗi khi xử lý task gỡ role {role_name} cho user {user_id}: {e}")

# Lớp Select Menu cho lệnh $store
class FileSelectMenu(discord.ui.Select):
    def __init__(self, files):
        options = [
            discord.SelectOption(
                label=f"{file['file_name']} (ID: {file['file_id']})",
                value=file['file_id']
            ) for file in files
        ]
        super().__init__(placeholder="Chọn một tệp...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        file_id = self.values[0]
        file = file_uploads_collection.find_one({"file_id": file_id})
        if file:
            embed = discord.Embed(
                title="Thông tin tệp",
                description=f"Tên tệp: {file['file_name']}\n"
                            f"File ID: {file['file_id']}\n"
                            f"Link Drive: {file['drive_link']}\n"
                            f"Upload bởi: <@{file['uploader_id']}>\n"
                            f"Thời gian upload: {file['upload_time'].strftime('%H:%M %d/%m/%Y UTC')}",
                color=discord.Color.blue()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message("Không tìm thấy tệp!", ephemeral=True)

class FileSelectView(discord.ui.View):
    def __init__(self, files):
        super().__init__(timeout=60)
        self.add_item(FileSelectMenu(files))

@bot.event
async def on_ready():
    """Sự kiện khi bot khởi động."""
    logger.info(f"Bot đã sẵn sàng với tên {bot.user}")
    if not bot.guilds:
        logger.error("Bot không tham gia server nào!")
        return
    guild = bot.guilds[0]
    if not guild.me.guild_permissions.manage_roles:
        logger.error("Bot thiếu quyền Manage Roles trong server!")
    if not guild.me.guild_permissions.manage_channels:
        logger.error("Bot thiếu quyền Manage Channels trong server!")
    active_tasks = {}
    for record in role_timers_collection.find():
        user_id = record["user_id"]
        role_name = record["role_name"]
        expiration_time = record["expiration_time"]
        if expiration_time > datetime.utcnow():
            key = f"{user_id}_{role_name}"
            if key not in active_tasks:
                member = guild.get_member(user_id)
                role = discord.utils.get(guild.roles, name=role_name)
                if member and role and role in member.roles:
                    task = asyncio.create_task(remove_role_after_delay(member, role, user_id, role_name))
                    active_tasks[key] = task
    check_role_expirations.start()

@bot.event
async def on_guild_remove(guild):
    """Xử lý khi bot rời server."""
    role_timers_collection.delete_many({"guild_id": guild.id})
    role_history_collection.delete_many({"guild_id": guild.id})
    file_uploads_collection.delete_many({"guild_id": guild.id})
    download_history_collection.delete_many({"guild_id": guild.id})
    logger.info(f"Bot đã rời server {guild.name} ({guild.id}), dọn dẹp dữ liệu MongoDB.")

@bot.command()
@commands.check(lambda ctx: has_role(ctx.author, ADMIN_ROLES))
async def giahan(ctx):
    """Gia hạn hoặc cấp mới role xem sếch cho người dùng."""
    if len(ctx.message.mentions) != 1:
        await ctx.send(f"{ctx.author.mention}, vui lòng mention đúng một người!")
        return
    user = ctx.message.mentions[0]
    role_name = role_mapping[TIMED_ROLE_KEY]
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        await ctx.send(f"{ctx.author.mention}, role {role_name} chưa được tạo, vui lòng nhờ Admin tạo role!")
        return
    if not ctx.guild.me.guild_permissions.manage_roles:
        await ctx.send(f"{ctx.author.mention}, bot không có quyền Manage Roles! Vui lòng cấp quyền cho bot.")
        return
    if role.position >= ctx.guild.me.top_role.position:
        await ctx.send(f"{ctx.author.mention}, role {role_name} có thứ tự cao hơn role của bot! Vui lòng điều chỉnh thứ tự role.")
        return

    set_time = datetime.utcnow()
    record = role_timers_collection.find_one({"user_id": user.id, "role_name": role_name})
    if record and record["expiration_time"] > set_time:
        new_expiration_time = record["expiration_time"] + timedelta(days=ROLE_DURATION_DAYS)
        role_timers_collection.update_one(
            {"user_id": user.id, "role_name": role_name},
            {"$set": {
                "expiration_time": new_expiration_time,
                "last_notified": None
            }}
        )
        role_history_collection.insert_one({
            "user_id": user.id,
            "role_name": role_name,
            "set_time": set_time,
            "expiration_time": new_expiration_time,
            "action": "gia_han",
            "guild_id": ctx.guild.id
        })
        remaining_time = format_remaining_time(new_expiration_time)
        await ctx.send(f"{user.mention}, thời gian xem sếch đã được gia hạn thêm {ROLE_DURATION_DAYS} ngày, còn {remaining_time}!")
        notification_channel = bot.get_channel(ROLE_NOTIFICATION_CHANNEL_ID)
        if notification_channel:
            await notification_channel.send(
                f"Gia hạn role cho {user.mention} vào {set_time.strftime('%H:%M %d/%m/%Y UTC')} "
                f"với thời gian còn lại là {remaining_time}"
            )
    else:
        try:
            await user.add_roles(role)
            logger.info(f"Đã cấp role {role_name} cho {user.id}")
        except Exception as e:
            logger.error(f"Lỗi khi cấp role {role_name} cho {user.id}: {e}")
            await ctx.send(f"{ctx.author.mention}, không thể cấp role {role_name} cho {user.mention} do lỗi: {str(e)}")
            return
        expiration_time = set_time + timedelta(days=ROLE_DURATION_DAYS)
        role_timers_collection.update_one(
            {"user_id": user.id, "role_name": role_name},
            {"$set": {
                "set_time": set_time,
                "expiration_time": expiration_time,
                "last_notified": None,
                "guild_id": ctx.guild.id
            }},
            upsert=True
        )
        role_history_collection.insert_one({
            "user_id": user.id,
            "role_name": role_name,
            "set_time": set_time,
            "expiration_time": expiration_time,
            "action": "cap_moi",
            "guild_id": ctx.guild.id
        })
        remaining_time = format_remaining_time(expiration_time)
        await ctx.send(f"{user.mention}, bạn đã được cấp role xem sếch trong {ROLE_DURATION_DAYS} ngày!")
        notification_channel = bot.get_channel(ROLE_NOTIFICATION_CHANNEL_ID)
        if notification_channel:
            await notification_channel.send(
                f"Cấp role cho {user.mention} vào {set_time.strftime('%H:%M %d/%m/%Y UTC')} "
                f"với thời gian còn lại là {remaining_time}"
            )

    asyncio.create_task(remove_role_after_delay(user, role, user.id, role_name))

@bot.command()
@commands.check(lambda ctx: has_role(ctx.author, ADMIN_ROLES))
async def rm(ctx):
    """Gỡ role xem sếch khỏi người dùng."""
    if len(ctx.message.mentions) != 1:
        await ctx.send(f"{ctx.author.mention}, vui lòng mention đúng một người!")
        return
    user = ctx.message.mentions[0]
    role_name = role_mapping[TIMED_ROLE_KEY]
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        await ctx.send(f"{ctx.author.mention}, role {role_name} chưa được tạo, vui lòng nhờ Admin tạo role!")
        return
    if not ctx.guild.me.guild_permissions.manage_roles:
        await ctx.send(f"{ctx.author.mention}, bot không có quyền Manage Roles! Vui lòng cấp quyền cho bot.")
        return
    if role.position >= ctx.guild.me.top_role.position:
        await ctx.send(f"{ctx.author.mention}, role {role_name} có thứ tự cao hơn role của bot! Vui lòng điều chỉnh thứ tự role.")
        return
    if role in user.roles:
        try:
            await user.remove_roles(role)
            role_timers_collection.delete_one({"user_id": user.id, "role_name": role_name})
            await ctx.send(f"{ctx.author.mention}, đã gỡ role {role_name} khỏi {user.mention}!")
            notification_channel = bot.get_channel(ROLE_NOTIFICATION_CHANNEL_ID)
            if notification_channel:
                await notification_channel.send(f"{user.mention}, role xem sếch của bạn đã bị gỡ, vui lòng liên hệ Admin!")
            logger.info(f"Đã gỡ role {role_name} khỏi {user.id}")
        except Exception as e:
            logger.error(f"Lỗi khi gỡ role {role_name} cho {user.id}: {e}")
            await ctx.send(f"{ctx.author.mention}, không thể gỡ role {role_name} khỏi {user.mention} do lỗi: {str(e)}")
    else:
        await ctx.send(f"{ctx.author.mention}, {user.mention} không có role {role_name} để gỡ!")

@bot.command()
async def check(ctx, user: discord.Member = None):
    """Kiểm tra thời gian role xem sếch của bản thân hoặc người khác (Admin/Mod/Friendly Dev)."""
    if user is None:
        user = ctx.author
    else:
        if not has_role(ctx.author, ADMIN_ROLES):
            await ctx.send(f"{ctx.author.mention}, bạn không có quyền kiểm tra thời gian của người khác! Hãy dùng `$check` để kiểm tra thời gian của chính bạn.")
            return
    role_name = role_mapping[TIMED_ROLE_KEY]
    record = role_timers_collection.find_one({"user_id": user.id, "role_name": role_name})
    if record and record["expiration_time"] > datetime.utcnow():
        expiration_time = record["expiration_time"]
        remaining = format_remaining_time(expiration_time)
        await ctx.send(f"{user.mention} còn {remaining} để xem sếch!")
    else:
        await ctx.send(f"{user.mention} chưa có role xem sếch, vui lòng nạp VIP lên mâm 1 để có thể coi sếch!")

@bot.command()
@commands.check(lambda ctx: has_role(ctx.author, ADMIN_ROLES))
async def log(ctx, user: discord.Member = None):
    """Kiểm tra lịch sử gia hạn role của người dùng."""
    if user is None:
        await ctx.send(f"{ctx.author.mention}, vui lòng mention một người để kiểm tra lịch sử gia hạn!")
        return
    role_name = role_mapping[TIMED_ROLE_KEY]
    history = role_history_collection.find({"user_id": user.id, "role_name": role_name}).sort("set_time", 1)
    history_list = []
    for record in history:
        set_time = record["set_time"].strftime('%H:%M %d/%m/%Y UTC')
        expiration_time = record["expiration_time"].strftime('%H:%M %d/%m/%Y UTC')
        action = "Cấp mới" if record["action"] == "cap_moi" else "Gia hạn"
        history_list.append(f"- {action} vào {set_time}, hết hạn vào {expiration_time}")
    if history_list:
        await ctx.send(f"Lịch sử gia hạn role {role_name} của {user.mention}:\n" + "\n".join(history_list))
    else:
        await ctx.send(f"{user.mention} chưa có lịch sử gia hạn role {role_name}!")

@bot.command()
@commands.check(lambda ctx: has_role(ctx.author, ADMIN_ROLES))
async def upload(ctx):
    """Upload file zip/rar lên Google Drive (Admin/Mod/Friendly Dev)."""
    if not ctx.message.attachments:
        await ctx.send(f"{ctx.author.mention}, vui lòng đính kèm một file zip hoặc rar!")
        return
    attachment = ctx.message.attachments[0]
    if not attachment.filename.lower().endswith(('.zip', '.rar')):
        await ctx.send(f"{ctx.author.mention}, chỉ hỗ trợ file zip hoặc rar!")
        return
    if attachment.size > 100 * 1024 * 1024:  # 100MB
        await ctx.send(f"{ctx.author.mention}, file quá lớn (>100MB)! Vui lòng chia nhỏ file.")
        return

    await ctx.send(f"{ctx.author.mention}, đang upload file lên Google Drive...")
    file_data = await attachment.read()
    file_id, drive_link = await upload_to_drive(file_data, attachment.filename)
    if not file_id:
        await ctx.send(f"{ctx.author.mention}, lỗi khi upload file! Kiểm tra DRIVE_FOLDER_ID hoặc quyền thư mục Google Drive.")
        return

    unique_file_id = str(uuid.uuid4())
    file_uploads_collection.insert_one({
        "file_id": unique_file_id,
        "file_name": attachment.filename,
        "upload_time": datetime.utcnow(),
        "drive_link": drive_link,
        "uploader_id": ctx.author.id,
        "guild_id": ctx.guild.id,
        "drive_file_id": file_id  # Lưu ID file trên Google Drive
    })
    await ctx.send(
        f"{ctx.author.mention}, upload thành công!\n"
        f"Tên tệp: {attachment.filename}\n"
        f"File ID: {unique_file_id}\n"
        f"Link Drive: {drive_link}"
    )

@bot.command()
async def get(ctx, *, file_name):
    """Tra cứu file_id dựa trên tên tệp."""
    files = file_uploads_collection.find({"$text": {"$search": file_name}})
    file_list = list(files)
    if not file_list:
        await ctx.send(f"{ctx.author.mention}, không tìm thấy tệp nào khớp với '{file_name}'!")
        return
    response = [f"- {file['file_name']} (ID: {file['file_id']})" for file in file_list]
    await ctx.send(f"Kết quả tìm kiếm cho '{file_name}':\n" + "\n".join(response[:25]))

@bot.command()
async def download(ctx, file_id: str):
    """Tải tệp từ Google Drive, gửi vào kênh tạm thời."""
    file = file_uploads_collection.find_one({"file_id": file_id})
    if not file:
        await ctx.send(f"{ctx.author.mention}, không tìm thấy tệp với ID '{file_id}'!")
        return

    category = bot.get_channel(FILE_CATEGORY_ID)
    if not category or not isinstance(category, discord.CategoryChannel):
        await ctx.send(f"{ctx.author.mention}, danh mục kênh không hợp lệ! Vui lòng cấu hình FILE_CATEGORY_ID.")
        return
    if not ctx.guild.me.guild_permissions.manage_channels:
        await ctx.send(f"{ctx.author.mention}, bot không có quyền Manage Channels! Vui lòng cấp quyền.")
        return

    await ctx.send(f"{ctx.author.mention}, đang tải tệp từ Google Drive...")
    file_data = await download_from_drive(file['drive_file_id'])
    if not file_data:
        await ctx.send(f"{ctx.author.mention}, lỗi khi tải tệp từ Google Drive!")
        return

    channel = await ctx.guild.create_text_channel(
        name=f"download-{ctx.author.id}-{int(datetime.utcnow().timestamp())}",
        category=category,
        overwrites={
            ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            ctx.author: discord.PermissionOverwrite(view_channel=True, send_messages=True)
        }
    )
    download_id = str(uuid.uuid4())
    download_history_collection.insert_one({
        "download_id": download_id,
        "user_id": ctx.author.id,
        "user_name": ctx.author.name,
        "file_id": file_id,
        "file_name": file['file_name'],
        "download_time": datetime.utcnow(),
        "guild_id": ctx.guild.id
    })
    await channel.send(
        f"{ctx.author.mention}, tệp của bạn đã sẵn sàng!",
        file=discord.File(file_data, filename=file['file_name'])
    )
    await ctx.send(f"{ctx.author.mention}, tệp đã được gửi đến kênh {channel.mention}! Kênh sẽ tự xóa sau 5 phút.")
    await asyncio.sleep(300)  # 5 phút
    try:
        await channel.delete()
        logger.info(f"Đã xóa kênh tạm {channel.id}")
    except Exception as e:
        logger.error(f"Lỗi khi xóa kênh {channel.id}: {e}")

@bot.command()
async def store(ctx, *, keyword=None):
    """Tìm kiếm tệp theo từ khóa và hiển thị select menu."""
    if not keyword:
        await ctx.send(f"{ctx.author.mention}, vui lòng nhập từ khóa tìm kiếm! Ví dụ: `$store Classmate`")
        return
    try:
        files = file_uploads_collection.find({"$text": {"$search": f"\"{keyword}\""}}).sort("upload_time", -1)
        file_list = list(files)[:25]
        if not file_list:
            await ctx.send(f"{ctx.author.mention}, không tìm thấy tệp nào khớp với '{keyword}'!")
            return
        view = FileSelectView(file_list)
        await ctx.send(f"Kết quả tìm kiếm cho '{keyword}':", view=view)
    except Exception as e:
        logger.error(f"Lỗi khi thực thi $store: {e}")
        await ctx.send(f"{ctx.author.mention}, có lỗi xảy ra khi tìm kiếm. Vui lòng kiểm tra log hoặc liên hệ Admin!")

@bot.command()
@commands.check(lambda ctx: has_role(ctx.author, ADMIN_ROLES))
async def q(ctx, download_id: str):
    """Kiểm tra thông tin lượt download (Admin/Mod/Friendly Dev)."""
    record = download_history_collection.find_one({"download_id": download_id})
    if not record:
        await ctx.send(f"{ctx.author.mention}, không tìm thấy lượt download với ID '{download_id}'!")
        return
    embed = discord.Embed(
        title="Thông tin lượt download",
        description=f"Download ID: {record['download_id']}\n"
                    f"Người dùng: <@{record['user_id']}> ({record['user_name']})\n"
                    f"Tên tệp: {record['file_name']}\n"
                    f"File ID: {record['file_id']}\n"
                    f"Thời gian: {record['download_time'].strftime('%H:%M %d/%m/%Y UTC')}",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

@bot.command()
async def help(ctx):
    """Hiển thị danh sách lệnh và mô tả cách sử dụng."""
    embed = discord.Embed(title="Hướng dẫn sử dụng bot", color=discord.Color.blue())
    if has_role(ctx.author, ADMIN_ROLES):
        embed.add_field(
            name="Lệnh cho Admin/Mod/Friendly Dev",
            value=(
                "**$giahan @user**: Gia hạn hoặc cấp mới role xem sếch cho người dùng (50 ngày).\n"
                "**$rm @user**: Gỡ role xem sếch khỏi người dùng.\n"
                "**$check [@user]**: Kiểm tra thời gian role xem sếch của bản thân hoặc người khác.\n"
                "**$log @user**: Xem lịch sử gia hạn role xem sếch của người dùng.\n"
                "**$upload**: Upload file zip/rar lên Google Drive (đính kèm file).\n"
                "**$q <download_id>**: Kiểm tra thông tin lượt download theo ID.\n"
                "**$get <tên tệp>**: Tìm file_id theo tên tệp (ví dụ: `$get Classmate`).\n"
                "**$download <file_id>**: Tải tệp từ Google Drive, gửi vào kênh tạm thời.\n"
                "**$store <keyword>**: Tìm kiếm tệp theo từ khóa và chọn từ menu (ví dụ: `$store Classmate`)."
            ),
            inline=False
        )
    else:
        embed.add_field(
            name="Lệnh cho người dùng",
            value=(
                "**$check**: Kiểm tra thời gian role xem sếch của bạn.\n"
                "**$get <tên tệp>**: Tìm file_id theo tên tệp (ví dụ: `$get Classmate`).\n"
                "**$download <file_id>**: Tải tệp từ Google Drive, gửi vào kênh tạm thời.\n"
                "**$store <keyword>**: Tìm kiếm tệp theo từ khóa và chọn từ menu (ví dụ: `$store Classmate`)."
            ),
            inline=False
        )
    await ctx.send(embed=embed)

@tasks.loop(hours=1)
async def check_role_expirations():
    """Kiểm tra role hết hạn và thông báo."""
    try:
        guild = bot.guilds[0]
        notification_channel = bot.get_channel(ROLE_NOTIFICATION_CHANNEL_ID)
        if not notification_channel:
            logger.warning("Không tìm thấy kênh thông báo thời gian còn lại!")
            return
        current_time = datetime.utcnow()
        for record in role_timers_collection.find():
            user_id = record["user_id"]
            role_name = record["role_name"]
            expiration_time = record["expiration_time"]
            last_notified = record.get("last_notified")
            remaining_time = expiration_time - current_time
            remaining_seconds = remaining_time.total_seconds()
            if 0 < remaining_seconds < NOTIFICATION_THRESHOLD_DAYS * 24 * 3600:
                if last_notified is None or (current_time - last_notified).total_seconds() >= 24 * 3600:
                    formatted_time = format_remaining_time(expiration_time)
                    member = guild.get_member(user_id)
                    if member:
                        await notification_channel.send(
                            f"{member.mention}, bạn chỉ còn {formatted_time} để xem sếch, nhớ gia hạn nhé!"
                        )
                        role_timers_collection.update_one(
                            {"user_id": user_id, "role_name": role_name},
                            {"$set": {"last_notified": current_time}}
                        )
    except Exception as e:
        logger.error(f"Lỗi khi kiểm tra role hết hạn: {e}")

@bot.event
async def on_command_error(ctx, error):
    """Xử lý lỗi lệnh."""
    if isinstance(error, commands.CommandNotFound):
        logger.info(f"Lệnh không tồn tại: {ctx.message.content}")
        return
    if isinstance(error, commands.MissingRole) or isinstance(error, commands.CheckFailure):
        await ctx.send(f"{ctx.author.mention}, bạn không có quyền sử dụng lệnh này!")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send(f"{ctx.author.mention}, không tìm thấy người dùng! Vui lòng mention một người dùng hợp lệ (ví dụ: @user).")
    else:
        logger.error(f"Lỗi lệnh: {error}")
        await ctx.send(f"{ctx.author.mention}, có lỗi xảy ra: {str(error)}. Vui lòng liên hệ Admin.")

# Chạy bot
bot.run(DISCORD_TOKEN)
