import discord
from discord.ext import commands, tasks
import os
from pymongo import MongoClient
from datetime import datetime, timedelta
import logging
import asyncio
import re
from dotenv import load_dotenv

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
ROLE_NOTIFICATION_CHANNEL_ID = int(os.getenv("ROLE_NOTIFICATION_CHANNEL_ID"))

# Thiết lập bot Discord
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="$", intents=intents)

# Thiết lập MongoDB
try:
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo_client.server_info()
    db = mongo_client["discord_bot_db"]
    role_timers_collection = db["role_timers"]
    role_history_collection = db["role_history"]
except Exception as e:
    logger.error(f"Không thể kết nối MongoDB: {e}")
    raise Exception(f"Không thể kết nối MongoDB: {e}")

# Ánh xạ role
role_mapping = {
    "giahan_new": "Gia hạn"    # Role chính cho $giahan
}
TIMED_ROLE_KEY = "giahan_new"  # Role quản lý thời gian

# Hàm kiểm tra role
def has_role(member, role_names):
    return any(role.name in role_names for role in member.roles)

# Hàm định dạng thời gian còn lại
def format_remaining_time(expiration_time):
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

# Hàm xóa role sau thời gian hết hạn (cập nhật với logic kiểm tra mới)
async def remove_role_after_delay(member, role, user_id, role_name):
    try:
        # Kiểm tra ngay lập tức
        record = role_timers_collection.find_one({"user_id": user_id, "role_name": role_name})
        if record:
            expiration_time = record["expiration_time"]
            remaining = expiration_time - datetime.utcnow()
            duration = remaining.total_seconds()
            if duration <= 0:
                # Hết hạn ngay, xóa
                await member.remove_roles(role)
                role_timers_collection.delete_one({"user_id": user_id, "role_name": role_name})
                channel = bot.get_channel(ROLE_NOTIFICATION_CHANNEL_ID)
                if channel:
                    await channel.send(f"{member.mention}, bạn đã hết giờ xem sếch, vui lòng liên hệ Admin!")
                return
            else:
                # Sleep đến hết hạn
                await asyncio.sleep(duration)
                
                # Sau sleep, kiểm tra lại DB
                new_record = role_timers_collection.find_one({"user_id": user_id, "role_name": role_name})
                if not new_record or new_record["expiration_time"] <= datetime.utcnow():
                    # Vẫn hết hạn, xóa
                    await member.remove_roles(role)
                    role_timers_collection.delete_one({"user_id": user_id, "role_name": role_name})
                    channel = bot.get_channel(ROLE_NOTIFICATION_CHANNEL_ID)
                    if channel:
                        await channel.send(f"{member.mention}, bạn đã hết giờ xem sếch, vui lòng liên hệ Admin!")
                else:
                    # Kiểm tra thời gian còn lại mới
                    new_expiration_time = new_record["expiration_time"]
                    new_remaining = new_expiration_time - datetime.utcnow()
                    new_duration = new_remaining.total_seconds()
                    if new_duration > 5 * 24 * 3600:  # > 5 ngày
                        # Bỏ qua, không xóa
                        logger.info(f"Task xóa role cho {user_id} bị bỏ qua vì còn {new_duration / (24*3600):.1f} ngày")
                        return
                    else:
                        # Tạo task mới để theo dõi nếu <= 5 ngày
                        asyncio.create_task(remove_role_after_delay(member, role, user_id, role_name))
                        logger.info(f"Tạo task mới cho {user_id} vì còn {new_duration / (24*3600):.1f} ngày")
        else:
            # Không có record, không làm gì
            pass
    except Exception as e:
        logger.error(f"Lỗi khi xử lý task gỡ role {role_name} cho user {user_id}: {e}")

@bot.event
async def on_ready():
    logger.info(f"Bot đã sẵn sàng với tên {bot.user}")
    # Khôi phục các task gỡ role từ MongoDB
    for record in role_timers_collection.find():
        user_id = record["user_id"]
        role_name = record["role_name"]
        expiration_time = record["expiration_time"]
        if expiration_time > datetime.utcnow():
            guild = bot.guilds[0]
            member = guild.get_member(user_id)
            role = discord.utils.get(guild.roles, name=role_name)
            if member and role and role in member.roles:
                asyncio.create_task(remove_role_after_delay(member, role, user_id, role_name))
    check_role_expirations.start()

@bot.command()
@commands.check(lambda ctx: has_role(ctx.author, ["Admin", "Mod", "Friendly Dev"]))
async def giahan(ctx, *args):
    # Parse số ngày từ invoked_with (ví dụ 'giahan' hoặc 'giahan100')
    command_name = ctx.invoked_with.lower()
    if command_name == 'giahan':
        days = 50
    else:
        # Cố gắng lấy số sau 'giahan'
        match = re.match(r'giahan(\d+)', command_name)
        if match:
            try:
                days = int(match.group(1))
                if days <= 0:
                    await ctx.send(f"{ctx.author.mention}, số ngày phải lớn hơn 0!")
                    return
            except ValueError:
                await ctx.send(f"{ctx.author.mention}, định dạng lệnh không hợp lệ! Sử dụng $giahan hoặc $giahanX (X là số ngày).")
                return
        else:
            await ctx.send(f"{ctx.author.mention}, định dạng lệnh không hợp lệ! Sử dụng $giahan hoặc $giahanX (X là số ngày).")
            return

    # Kiểm tra mention
    if len(ctx.message.mentions) != 1:
        await ctx.send(f"{ctx.author.mention}, vui lòng mention đúng một người!")
        return
    user = ctx.message.mentions[0]
    role_name = role_mapping[TIMED_ROLE_KEY]
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        await ctx.send(f"{ctx.author.mention}, role {role_name} chưa được tạo, vui lòng nhờ Admin tạo role!")
        return
    # Kiểm tra quyền Manage Roles của bot
    if not ctx.guild.me.guild_permissions.manage_roles:
        await ctx.send(f"{ctx.author.mention}, bot không có quyền Manage Roles! Vui lòng cấp quyền cho bot.")
        return
    # Kiểm tra thứ tự role
    if role.position >= ctx.guild.me.top_role.position:
        await ctx.send(f"{ctx.author.mention}, role {role_name} có thứ tự cao hơn role của bot! Vui lòng điều chỉnh thứ tự role.")
        return

    set_time = datetime.utcnow()
    record = role_timers_collection.find_one({"user_id": user.id, "role_name": role_name})
    if record and record["expiration_time"] > set_time:
        # Người dùng đã có role và còn thời gian
        new_expiration_time = record["expiration_time"] + timedelta(days=days)
        role_timers_collection.update_one(
            {"user_id": user.id, "role_name": role_name},
            {"$set": {
                "expiration_time": new_expiration_time,
                "last_notified": None
            }}
        )
        # Lưu lịch sử gia hạn
        role_history_collection.insert_one({
            "user_id": user.id,
            "role_name": role_name,
            "set_time": set_time,
            "expiration_time": new_expiration_time,
            "action": "gia_han"
        })
        remaining_time = format_remaining_time(new_expiration_time)
        await ctx.send(f"{user.mention}, thời gian bạn có thể xem sếch đã được gia hạn thêm {days} ngày, còn {remaining_time}!")
        notification_channel = bot.get_channel(ROLE_NOTIFICATION_CHANNEL_ID)
        if notification_channel:
            await notification_channel.send(
                f"Gia hạn role cho {user.mention} vào {set_time.strftime('%H:%M %d/%m/%Y UTC')} "
                f"với thời gian còn lại là {remaining_time}"
            )
    else:
        # Người dùng chưa có role hoặc role đã hết hạn
        try:
            await user.add_roles(role)
            logger.info(f"Đã cấp role {role_name} cho {user.id}")
        except Exception as e:
            logger.error(f"Lỗi khi cấp role {role_name} cho {user.id}: {e}")
            await ctx.send(f"{ctx.author.mention}, không thể cấp role {role_name} cho {user.mention} do lỗi: {str(e)}")
            return
        expiration_time = set_time + timedelta(days=days)
        role_timers_collection.update_one(
            {"user_id": user.id, "role_name": role_name},
            {"$set": {
                "set_time": set_time,
                "expiration_time": expiration_time,
                "last_notified": None
            }},
            upsert=True
        )
        # Lưu lịch sử gia hạn
        role_history_collection.insert_one({
            "user_id": user.id,
            "role_name": role_name,
            "set_time": set_time,
            "expiration_time": expiration_time,
            "action": "cap_moi"
        })
        remaining_time = format_remaining_time(expiration_time)
        await ctx.send(f"{user.mention}, bạn đã được cấp quyền xem sếch trong {days} ngày!")
        notification_channel = bot.get_channel(ROLE_NOTIFICATION_CHANNEL_ID)
        if notification_channel:
            await notification_channel.send(
                f"Gia hạn role cho {user.mention} vào {set_time.strftime('%H:%M %d/%m/%Y UTC')} "
                f"với thời gian còn lại là {remaining_time}"
            )

    # Tạo task gỡ role
    asyncio.create_task(remove_role_after_delay(user, role, user.id, role_name))

@bot.command()
@commands.check(lambda ctx: has_role(ctx.author, ["Admin", "Mod", "Friendly Dev"]))
async def rm(ctx):
    if len(ctx.message.mentions) != 1:
        await ctx.send(f"{ctx.author.mention}, vui lòng mention đúng một người!")
        return
    user = ctx.message.mentions[0]
    role_name = role_mapping[TIMED_ROLE_KEY]
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        await ctx.send(f"{ctx.author.mention}, role {role_name} chưa được tạo, vui lòng nhờ Admin tạo role!")
        return
    # Kiểm tra quyền Manage Roles của bot
    if not ctx.guild.me.guild_permissions.manage_roles:
        await ctx.send(f"{ctx.author.mention}, bot không có quyền Manage Roles! Vui lòng cấp quyền cho bot.")
        return
    # Kiểm tra thứ tự role
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
                await notification_channel.send(f"{user.mention}, thời gian xem sếch của bạn đã hết, vui lòng nạp VIP để lên mâm 1!")
            logger.info(f"Đã gỡ role {role_name} khỏi {user.id}")
        except Exception as e:
            logger.error(f"Lỗi khi gỡ role {role_name} cho {user.id}: {e}")
            await ctx.send(f"{ctx.author.mention}, không thể gỡ role {role_name} khỏi {user.mention} do lỗi: {str(e)}")
    else:
        await ctx.send(f"{ctx.author.mention}, {user.mention} không có role {role_name} để gỡ!")

@bot.command()
async def check(ctx, user: discord.Member = None):
    if user is None:
        user = ctx.author
    else:
        if not has_role(ctx.author, ["Admin", "Mod", "Friendly Dev"]):
            await ctx.send(f"{ctx.author.mention}, bạn không có quyền kiểm tra thời gian của người khác! Hãy dùng `$check` để kiểm tra thời gian của chính bạn.")
            return
    role_name = role_mapping[TIMED_ROLE_KEY]
    record = role_timers_collection.find_one({"user_id": user.id, "role_name": role_name})
    if record and record["expiration_time"] > datetime.utcnow():
        expiration_time = record["expiration_time"]
        remaining = format_remaining_time(expiration_time)
        await ctx.send(f"Bạn còn {remaining} để xem sếch!")
    else:
        await ctx.send(f"Vui lòng nạp VIP lên mâm 1 để có thể coi sếch!")

@bot.command()
@commands.check(lambda ctx: has_role(ctx.author, ["Admin", "Mod", "Friendly Dev"]))
async def log(ctx, user: discord.Member = None):
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

@tasks.loop(minutes=10)
async def check_role_expirations():
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
            if 0 < remaining_seconds < 5 * 24 * 3600:
                if last_notified is None or (current_time - last_notified).total_seconds() >= 24 * 3600:
                    formatted_time = format_remaining_time(expiration_time)
                    member = guild.get_member(user_id)
                    if member:
                        await notification_channel.send(
                            f"Này {member.mention}, bạn chỉ còn {formatted_time} để xem sếch thôi, nhớ gia hạn nhé!"
                        )
                        role_timers_collection.update_one(
                            {"user_id": user_id, "role_name": role_name},
                            {"$set": {"last_notified": current_time}}
                        )
    except Exception as e:
        logger.error(f"Lỗi khi kiểm tra role hết hạn: {e}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        logger.info(f"Lệnh không tồn tại: {ctx.message.content}")
        return
    if isinstance(error, commands.MissingRole):
        await ctx.send(f"{ctx.author.mention}, bạn không có quyền sử dụng lệnh này!")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send(f"{ctx.author.mention}, không tìm thấy người dùng! Vui lòng mention một người dùng hợp lệ (ví dụ: @user).")
    else:
        logger.error(f"Lỗi lệnh: {error}")
        await ctx.send(f"{ctx.author.mention}, có lỗi xảy ra: {str(error)}. Vui lòng liên hệ Admin.")

# Chạy bot
bot.run(DISCORD_TOKEN)

