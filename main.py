import asyncio
from datetime import datetime, time, timedelta, timezone
import json
import os
import random
import re
import typing
import urllib.parse
from dotenv import load_dotenv

from bs4 import BeautifulSoup
import discord
from discord import app_commands
from discord.ext import commands, tasks
from google import genai
import requests
import yt_dlp

# ==========================================
# ⏰ 한국 표준시(KST) 및 시간 설정
# ==========================================
KST = timezone(timedelta(hours=9))

# ==========================================
# ⚙️ 환경 변수 로드 (.env 파일)
# ==========================================
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TOKEN or not GEMINI_API_KEY:
    raise ValueError("🚨 .env 파일에 DISCORD_TOKEN 또는 GEMINI_API_KEY가 설정되지 않았습니다.")

# ⚙️ 봇 및 AI 클라이언트 생성
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# ==========================================
# 📁 1. 통합 서버 채널 데이터 저장/불러오기
# ==========================================
CHANNEL_CONFIG_FILE = "server_channels.json"

def load_channel_configs():
    """파일에서 저장된 서버별 특수 채널 설정을 불러옵니다."""
    if os.path.exists(CHANNEL_CONFIG_FILE):
        try:
            with open(CHANNEL_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {int(k): v for k, v in data.items()}
        except Exception as e:
            print(f"⚠️ 채널 설정 로드 실패: {e}")
    return {}

def save_channel_configs(data):
    """서버별 특수 채널 설정을 파일에 저장합니다."""
    try:
        with open(CHANNEL_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"⚠️ 채널 설정 저장 실패: {e}")

# ==========================================
# 🌐 2. 전역 변수 선언 구역
# ==========================================
server_channels = load_channel_configs()

# 음악 채널 변수 자동 동기화
music_channel_ids = {
    guild_id: config["player"]
    for guild_id, config in server_channels.items()
    if "player" in config
}

music_data = {}
player_messages = {}
inactivity_tasks = {}
INACTIVITY_TIMEOUT = 300  # 5분 무응답 시 자동 퇴장

# ==========================================
# 🎵 3. 음악 다운로드 및 FFmpeg 설정
# ==========================================
DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': f'{DOWNLOAD_DIR}/%(id)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'web']
        }
    }
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

FFMPEG_OPTIONS = {
    'options': '-vn'
}

# ==========================================
# 🎵 음악 데이터 및 플레이어 카드 관리 함수
# ==========================================
def get_music_data(guild_id):
    if guild_id not in music_data:
        music_data[guild_id] = {
            'queue': [],
            'current': None,
            'channel': None,
            'volume': 0.5,
            'history': [],
            'smart_dj': False
        }
    return music_data[guild_id]

async def send_or_update_player_embed(guild, channel):
    """채널에 단 1개의 플레이어 임베드만 유지하며 '수정(edit)'하는 함수"""
    mdata = get_music_data(guild.id)
    current = mdata.get("current")
    queue = mdata.get("queue", [])
    
    dj_status = "🤖 스마트 DJ ON" if mdata.get('smart_dj') else "🤖 스마트 DJ OFF"
    embed = discord.Embed(color=0x2ECC71 if current else 0x34495E)

    if current:
        embed.title = "🎵 [후후 음악실] 지금 재생 중"
        embed.description = (
            f"**[{current['title']}]**\n\n"
            f"• **재생 시간:** `{current['duration']}`\n"
            f"• **요청자:** {current['requester'].mention}"
        )
        if current.get("thumbnail"):
            embed.set_image(url=current["thumbnail"])
    else:
        embed.title = "⏹️ [후후 음악실] 대기 중"
        embed.description = (
            "현재 재생 중인 음악이 없습니다.\n\n"
            "💬 **사용 방법:**\n"
            "이 채널 채팅창에 **노래 제목**이나 **링크(유튜브, 스포티파이, 애플뮤직)**를 입력해 주세요!\n"
            "유저 메시지는 즉시 자동 삭제되며 플레이어 카드가 업데이트됩니다."
        )
        
        default_banner_url = "https://cdn.discordapp.com/attachments/1497493063960891583/1532149725934129332/9d805abf-532c-481f-b224-13892686554d.png?ex=6a6bcd20&is=6a6a7ba0&hm=5c7220159035db0386d97c5b7b558fe2d690078cc2581de4c73fdce92718b9b2&"
        if default_banner_url and default_banner_url.startswith(('http://', 'https://')):
            embed.set_image(url=default_banner_url)

    if queue:
        queue_str = ""
        for idx, song in enumerate(queue[:5], start=1):
            queue_str += f"**{idx}.** `{song['title']}` | 요청자: {song['requester'].display_name}\n"
        if len(queue) > 5:
            queue_str += f"*...외 {len(queue) - 5}곡 더 대기 중*\n"
        embed.add_field(name="📋 다음 재생 대기열", value=queue_str, inline=False)

    avatar_url = guild.me.display_avatar.url if guild.me else None
    embed.set_footer(text=f"후후 비서실 고음질 오디오 시스템 | {dj_status}", icon_url=avatar_url)
    
    view = MusicControlView()
    old_msg = player_messages.get(guild.id)

    # 💡 1. 메모리에 메시지가 없으면 채널에서 기존 봇 임베드 메시지 찾아내기
    if not old_msg:
        try:
            async for msg in channel.history(limit=15):
                if msg.author == bot.user and msg.embeds:
                    old_msg = msg
                    player_messages[guild.id] = old_msg
                    break
        except Exception:
            pass

    # 💡 2. 기존 메시지가 존재하면 수정(Edit)
    if old_msg:
        try:
            await old_msg.edit(embed=embed, view=view)
            return
        except (discord.NotFound, discord.HTTPException):
            player_messages.pop(guild.id, None)

    # 💡 3. 기존 메시지가 없을 때만 신규 전송
    try:
        new_msg = await channel.send(embed=embed, view=view)
        player_messages[guild.id] = new_msg
    except Exception as e:
        print(f"⚠️ 플레이어 메시지 전송 실패: {e}")

async def play_next(guild: discord.Guild, channel: discord.TextChannel):
    """음원을 로컬 다운로드 후 안정적으로 재생하며, 대기열 종료 시 스마트 DJ 자동 추천 실행"""
    mdata = get_music_data(guild.id)
    vc = guild.voice_client

    if not vc or not vc.is_connected():
        return

    song_info = None
    if mdata['queue']:
        song_info = mdata['queue'].pop(0)
    elif mdata.get('smart_dj', False):
        last_song = mdata['history'][-1] if mdata.get('history') else "최신 인기곡"
        try:
            prompt = f"'{last_song}'과 비슷한 분위기의 추천곡 1개의 '가수 - 노래제목'만 정확히 알려줘."
            response = await asyncio.to_thread(
                ai_client.models.generate_content,
                model='gemini-3.6-flash',
                contents=prompt
            )
            recommended_title = response.text.strip()
            search_query = f"ytsearch1:{recommended_title}"
            
            info = await asyncio.to_thread(ytdl.extract_info, search_query, download=False)
            if 'entries' in info and len(info['entries']) > 0:
                item = info['entries'][0]
                duration_sec = item.get('duration', 0)
                song_info = {
                    'title': item.get('title', recommended_title),
                    'webpage_url': item.get('webpage_url', f"https://www.youtube.com/watch?v={item['id']}"),
                    'duration': f"{duration_sec // 60}분 {duration_sec % 60}초",
                    'thumbnail': item.get('thumbnail'),
                    'requester': bot.user
                }
        except Exception as e:
            print(f"⚠️ 스마트 DJ 추천 오류: {e}")

    if not song_info:
        mdata['current'] = None
        await send_or_update_player_embed(guild, channel)
        return

    try:
        target_url = song_info.get('webpage_url') or song_info.get('url')
        info = await asyncio.to_thread(ytdl.extract_info, target_url, download=True)
        
        # 💡 entries 키가 존재할 때 항목이 비어있는지 먼저 안전하게 검사
        if 'entries' in info:
            if not info['entries']:
                raise ValueError("검색된 음원 결과가 비어 있습니다.")
            info = info['entries'][0]
        
        file_path = ytdl.prepare_filename(info)
        song_info['file_path'] = file_path
        
        mdata['current'] = song_info
        if 'history' not in mdata:
            mdata['history'] = []
        mdata['history'].append(song_info['title'])

    except Exception as e:
        print(f"⚠️ 음원 다운로드 실패: {e}")
        err_msg = await channel.send(f"❌ `{song_info['title']}` 음원을 찾을 수 없거나 다운로드 중 오류가 발생했습니다.")
        
        # 💡 3초 동안 유저에게 안내 후 자동 삭제 (실패 시 무시)
        await asyncio.sleep(3)
        try:
            await err_msg.delete()
        except Exception:
            pass

        await play_next(guild, channel)
        return


    try:
        audio_source = discord.FFmpegPCMAudio(song_info['file_path'], **FFMPEG_OPTIONS)
        audio_source = discord.PCMVolumeTransformer(audio_source, volume=mdata.get('volume', 0.5))

        def after_playing(error):
            if error:
                print(f"⚠️ 재생 완료 후 오류: {error}")
            
            if os.path.exists(song_info['file_path']):
                try:
                    os.remove(song_info['file_path'])
                except Exception as del_err:
                    print(f"⚠️ 파일 삭제 실패: {del_err}")

            asyncio.run_coroutine_threadsafe(play_next(guild, channel), bot.loop)

        vc.play(audio_source, after=after_playing)
        await send_or_update_player_embed(guild, channel)

    except Exception as e:
        print(f"⚠️ 재생 시도 오류: {e}")
        await play_next(guild, channel)

# ==========================================
# 🎛️ 음악 제어용 버튼 UI 클래스
# ==========================================
class MusicControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="일시정지/재생", style=discord.ButtonStyle.primary, emoji="⏯️")
    async def pause_resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            return await interaction.response.send_message("❌ 현재 재생 중인 음악이 없습니다.", ephemeral=True)
        
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ 음악을 일시정지했습니다.", ephemeral=True)
        else:
            vc.resume()
            await interaction.response.send_message("▶️ 음악을 다시 재생합니다.", ephemeral=True)
        
        await send_or_update_player_embed(interaction.guild, interaction.channel)

    @discord.ui.button(label="스킵", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await interaction.response.send_message("⏭️ 다음 곡으로 건너뜁니다.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ 스킵할 음악이 없습니다.", ephemeral=True)

    @discord.ui.button(label="대기열", style=discord.ButtonStyle.secondary, emoji="📜")
    async def queue_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        mdata = get_music_data(interaction.guild_id)
        current = mdata.get("current")
        queue = mdata.get("queue", [])

        if not current and not queue:
            return await interaction.response.send_message("📜 현재 대기열이 비어 있습니다.", ephemeral=True)

        embed = discord.Embed(title="📜 재생 대기열 목록", color=0x3498DB)
        if current:
            embed.add_field(
                name="▶️ 현재 재생 중",
                value=f"**[{current['title']}]** (`{current['duration']}`) - 요청자: {current['requester'].mention}",
                inline=False
            )

        if queue:
            queue_str = ""
            for idx, song in enumerate(queue[:10], start=1):
                queue_str += f"**{idx}.** `{song['title']}` (`{song['duration']}`) | {song['requester'].display_name}\n"
            if len(queue) > 10:
                queue_str += f"\n*...외 {len(queue) - 10}곡 더 남음*"
            embed.add_field(name="📋 다음 대기 곡 목록", value=queue_str, inline=False)
        else:
            embed.add_field(name="📋 다음 대기 곡 목록", value="대기 중인 곡이 없습니다.", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="스마트 DJ", style=discord.ButtonStyle.success, emoji="🤖")
    async def toggle_smart_dj_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        mdata = get_music_data(interaction.guild_id)
        current_status = mdata.get("smart_dj", False)
        mdata["smart_dj"] = not current_status
        
        status_msg = "🟢 **켜짐 (ON)**" if mdata["smart_dj"] else "⚪ **꺼짐 (OFF)**"
        await interaction.response.send_message(f"🤖 **스마트 DJ** 기능이 {status_msg} 상태로 변경되었습니다.", ephemeral=True)
        await send_or_update_player_embed(interaction.guild, interaction.channel)

    @discord.ui.button(label="퇴장", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        mdata = get_music_data(interaction.guild_id)
        
        mdata["queue"].clear()
        mdata["current"] = None

        if vc and vc.is_connected():
            await vc.disconnect()
            await interaction.response.send_message("👋 대기열을 비우고 퇴장했습니다.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ 봇이 음성 채널에 접속해 있지 않습니다.", ephemeral=True)

        await send_or_update_player_embed(interaction.guild, interaction.channel)

# ==========================================
# 🎵 macOS Opus 오디오 코덱 로드
# ==========================================
if not discord.opus.is_loaded():
    opus_paths = [
        "/opt/homebrew/lib/libopus.dylib",
        "/usr/local/lib/libopus.dylib",
        "libopus.dylib"
    ]
    for path in opus_paths:
        if os.path.exists(path):
            discord.opus.load_opus(path)
            print(f"✅ Opus 오디오 코덱 로드 성공: {path}")
            break

# ==========================================
# 💼 AI 여비서 기본 페르소나 설정
# ==========================================
AI_INSTRUCTION_DEFAULT = """
너의 이름은 '후후'이고, 이 디스코드 서버의 업무를 지원하는 무뚝뚝하고 냉철한 여비서 AI야.
1. 말투: 감정을 거의 드러내지 않는 단정하고 무덤덤한 존댓말(~습니다, ~입니다, ~해요)을 사용해. 과도한 이모지나 가벼운 은어('ㅋ', '헤헤' 등)는 절대 사용하지 마.
2. 성격: 오직 효율성과 사무적인 태도로 대하며, 불필요한 잡담이나 장난은 사절하지만 요청받은 업무는 완벽하고 신속하게 처리해.
3. 어조: 차갑고 조용하지만 예의 바르고 객관적인 여비서 톤을 유지해.
"""

# ==========================================
# 💾 데이터베이스 및 파일 설정 (JSON)
# ==========================================
DATA_FILE = "bank.json"
CONFIG_FILE = "server_config.json"
PERSONA_FILE = "user_personas.json"

user_bank = {}
server_config = {"music_channel_id": None, "levelup_channel_id": None, "briefing_channel_id": None}
user_personas = {}

def load_data():
    global user_bank
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                user_bank = {int(k): v for k, v in data.get("users", {}).items()}
        except Exception:
            user_bank = {}
    else:
        user_bank = {}

def save_data():
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"users": user_bank}, f, ensure_ascii=False, indent=4)

def check_user_data(user_id):
    if user_id not in user_bank or not isinstance(user_bank[user_id], dict):
        user_bank[user_id] = {
            "money": 10000, "last_date": "", "exp": 0, "level": 1, "points": 0, 
            "inventory": {"sniper_rifle": 0, "custom_role_ticket": 0}, "affinity": 50,
            "has_purchased_role": False, "titles": ["🌱 신입 사원"], "equipped_title": "🌱 신입 사원",
            "weekly_chat_count": 0
        }
    
    defaults = [
        ("money", 10000), ("last_date", ""), ("exp", 0), ("level", 1), 
        ("points", 0), ("affinity", 50), ("has_purchased_role", False),
        ("titles", ["🌱 신입 사원"]), ("equipped_title", "🌱 신입 사원"),
        ("weekly_chat_count", 0)
    ]
    for key, default in defaults:
        if key not in user_bank[user_id]: 
            user_bank[user_id][key] = default

def check_title_achievements(user_id):
    check_user_data(user_id)
    u_data = user_bank[user_id]
    
    my_titles = u_data.get("titles")
    if not my_titles:
        my_titles = ["🌱 신입 사원"]
        
    titles = set(my_titles)
    
    if u_data.get("level", 1) >= 10:
        titles.add("🚀 베테랑 사원")
    if u_data.get("money", 0) >= 1000000:
        titles.add("💰 서버 대부호")
    if u_data.get("points", 0) >= 10000:
        titles.add("💎 포인트 재벌")
    if u_data.get("affinity", 50) >= 85:
        titles.add("💼 전속 보좌관")
    if u_data.get("has_purchased_role", False):
        titles.add("🎨 아티스트")

    user_bank[user_id]["titles"] = list(titles)
    if "equipped_title" not in user_bank[user_id] or not user_bank[user_id]["equipped_title"]:
        user_bank[user_id]["equipped_title"] = "🌱 신입 사원"

def check_inventory(user_id):
    check_user_data(user_id)
    if "inventory" not in user_bank[user_id] or not isinstance(user_bank[user_id]["inventory"], dict):
        user_bank[user_id]["inventory"] = {"sniper_rifle": 0, "custom_role_ticket": 0}
    if "sniper_rifle" not in user_bank[user_id]["inventory"]:
        user_bank[user_id]["inventory"]["sniper_rifle"] = 0
    if "custom_role_ticket" not in user_bank[user_id]["inventory"]:
        user_bank[user_id]["inventory"]["custom_role_ticket"] = 0

def load_config():
    global server_config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                server_config = json.load(f)
        except Exception:
            server_config = {"music_channel_id": None, "levelup_channel_id": None, "briefing_channel_id": None}

def save_config():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(server_config, f, ensure_ascii=False, indent=4)

def load_personas():
    global user_personas
    if os.path.exists(PERSONA_FILE):
        try:
            with open(PERSONA_FILE, "r", encoding="utf-8") as f:
                user_personas = json.load(f)
        except Exception:
            user_personas = {}

def save_personas():
    with open(PERSONA_FILE, "w", encoding="utf-8") as f:
        json.dump(user_personas, f, ensure_ascii=False, indent=4)

def get_affinity_persona(user_id):
    check_user_data(user_id)
    affinity = user_bank[user_id]["affinity"]
    if affinity >= 85:
        return f"\n[현재 신뢰도: {affinity}/100 (최상)] 전속 보좌관으로서 깊은 신뢰를 표합니다. 무뚝뚝하지만 세심하고 철저하게 유저를 보좌하세요."
    elif affinity >= 60:
        return f"\n[현재 신뢰도: {affinity}/100 (양호)] 원활한 업무 관계입니다. 무덤덤하지만 친절하고 깔끔하게 안내하세요."
    elif affinity >= 35:
        return f"\n[현재 신뢰도: {affinity}/100 (보통)] 비즈니스적인 거리감이 있습니다. 단정하고 절제된 어조로 필요한 정보만 전달하세요."
    else:
        return f"\n[현재 신뢰도: {affinity}/100 (낮음)] 매우 차갑습니다. 업무 외 불필요한 대화는 최소화하고 단답형으로 응대하세요."

def get_affinity_bar(affinity):
    filled = int(affinity / 10)
    empty = 10 - filled
    return "🟦" * filled + "⬜" * empty

# ==========================================
# 🤖 봇 상태 및 루프 스케줄러
# ==========================================
@tasks.loop(seconds=3600)
async def change_status_loop():
    status_list = ["서버 데이터 관리 중", "업무 리포트 작성 중", "채널 메시지 모니터링", "명령어 대기 중"]
    await bot.change_presence(status=discord.Status.online, activity=discord.Activity(type=discord.ActivityType.playing, name=random.choice(status_list)))

MUSIC_TIME = time(hour=7, minute=0, tzinfo=KST)

@tasks.loop(time=MUSIC_TIME)
async def daily_song_recommendation():
    channel_id = server_config.get("music_channel_id")
    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    weekday_idx = datetime.now(KST).weekday()
    genre_schedule = [
        {"genre": "한국 노래 (K-POP/발라드)", "guide": "멜론 TOP 100 차트권 및 국내에서 대중적으로 사랑받는 대표 가요"},
        {"genre": "팝송 (POP)", "guide": "빌보드 HOT 100 및 글로벌하게 널리 알려진 대표 히트 팝송"},
        {"genre": "한국 랩/힙합 (국힙/R&B 랩)", "guide": "음원 차트 상위권 및 대중적인 국힙 명곡"},
        {"genre": "제이팝 (J-POP)", "guide": "오리콘 차트 상위권 및 인지도 높은 대표 J-POP"},
        {"genre": "한국 랩/힙합 (트렌디 힙합)", "guide": "주말을 앞두고 감각적인 비트의 대중적인 랩/힙합"},
        {"genre": "제이팝 (J-POP)", "guide": "감성 밴드 음악 또는 시티팝"},
        {"genre": "자유 선택", "guide": "한 주를 마무리하는 힐링 명곡"}
    ]
    
    today_info = genre_schedule[weekday_idx]

    prompt = (
        f"너는 철저하고 지적인 여비서 '후후'야. 오늘 사원들에게 추천할 음악 1곡을 보고해 줘.\n\n"
        f"[오늘의 지정 장르]: **{today_info['genre']}**\n"
        f"[선정 가이드라인]: {today_info['guide']}\n\n"
        "선정 및 작성 조건:\n"
        "1. 곡명 & 아티스트명 (원문과 한국어 표기 함께 작성)\n"
        "2. 장르 및 분위기\n"
        "3. 이 음악을 추천하는 이유 (2~3줄)\n"
        "4. 말투는 차분하고 격식 있는 여비서 어조(~습니다, ~입니다)를 유지할 것."
    )

    recommendation = await call_gemini(prompt, system_instruction=AI_INSTRUCTION_DEFAULT)

    if recommendation:
        embed = discord.Embed(
            title=f"🎵 [비서실 정기 보고] 오늘의 추천 음악 ({today_info['genre']})",
            description=recommendation,
            color=0x9B59B6,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="후후 비서실 제공", icon_url=bot.user.display_avatar.url)
        await channel.send(embed=embed)

@daily_song_recommendation.before_loop
async def before_daily_song():
    await bot.wait_until_ready()

WEEKLY_TIME = time(hour=7, minute=0, tzinfo=KST)

@tasks.loop(time=WEEKLY_TIME)
async def weekly_server_briefing():
    if datetime.now(KST).weekday() != 0:
        return

    channel_id = server_config.get("briefing_channel_id")
    if not channel_id or not user_bank:
        return

    channel = bot.get_channel(channel_id)
    if not channel:
        return

    sorted_users = sorted(user_bank.items(), key=lambda x: x[1].get("weekly_chat_count", 0), reverse=True)
    total_weekly_messages = sum(u.get("weekly_chat_count", 0) for u in user_bank.values())
    
    top_ranks = []
    for rank, (u_id, u_data) in enumerate(sorted_users[:3], 1):
        count = u_data.get("weekly_chat_count", 0)
        if count > 0:
            try:
                member = await bot.fetch_user(u_id)
                top_ranks.append(f"{rank}위: **{member.name}** 사원 (`{count:,}회` 작성)")
            except:
                pass

    top_ranks_str = "\n".join(top_ranks) if top_ranks else "지난 한 주 동안 기록된 활동이 없습니다."

    prompt = (
        "너는 무뚝뚝하고 철저한 여비서 '후후'야. 한 주간의 서버 주간 활동량 종합 보고서를 작성해 줘.\n\n"
        f"- 지난 주 서버 총 메시지 전송량: {total_weekly_messages:,}개\n"
        f"- 이번 주 명예의 전당 (활동왕 TOP 3):\n{top_ranks_str}\n\n"
        "작성 조건:\n"
        "1. 여비서 보고서 톤(~습니다, ~입니다)으로 명확하고 격식 있게 요약할 것."
    )

    report = await call_gemini(prompt, system_instruction=AI_INSTRUCTION_DEFAULT)

    if report:
        embed = discord.Embed(
            title="📊 [주간 결산] 서버 주간 활동량 보고서",
            description=report,
            color=0x3498DB,
            timestamp=discord.utils.utcnow()
        )
        if top_ranks:
            embed.add_field(name="👑 이번 주 주간 활동왕 (1위)", value=top_ranks[0], inline=False)
        embed.set_footer(text="매주 월요일 오전 7시 정기 결산", icon_url=bot.user.display_avatar.url)
        await channel.send(embed=embed)

    for u_id in user_bank:
        user_bank[u_id]["weekly_chat_count"] = 0
    save_data()

@bot.event
async def on_ready():
    load_data()
    load_config()
    load_personas()
    print(f"📋 여비서 '{bot.user.name}' 가동 준비 완료.")
    
    if not change_status_loop.is_running(): 
        change_status_loop.start()
    if not daily_song_recommendation.is_running():
        daily_song_recommendation.start()
    if not weekly_server_briefing.is_running():
        weekly_server_briefing.start()

    # 음악 신청 채널 플레이어 복구
    for guild_id, channel_id in music_channel_ids.items():
        guild = bot.get_guild(guild_id)
        if guild:
            channel = guild.get_channel(channel_id)
            if channel:
                mdata = get_music_data(guild_id)
                mdata["channel"] = channel
                await send_or_update_player_embed(guild, channel)

    try:
        synced = await bot.tree.sync()
        print(f"✨ 슬래시 명령어 {len(synced)}개 동기화 완료.")
    except Exception as e:
        print(f"🚨 동기화 오류: {e}")

async def call_gemini(prompt: str, system_instruction: str = None) -> str:
    """Gemini API 호출"""
    models_to_try = ['gemini-3.6-flash', 'gemini-3.1-pro']
    
    config = genai.types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=[{"google_search": {}}]
    ) if system_instruction else genai.types.GenerateContentConfig(tools=[{"google_search": {}}])

    for model_name in models_to_try:
        for attempt in range(3):
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        ai_client.models.generate_content,
                        model=model_name,
                        contents=prompt,
                        config=config
                    ),
                    timeout=30.0
                )
                if response and response.text:
                    return response.text
            except Exception as e:
                error_str = str(e)
                if "404" in error_str or "NOT_FOUND" in error_str:
                    break
                if "503" in error_str or "UNAVAILABLE" in error_str:
                    await asyncio.sleep(2)
                    continue
                break
            
    return None

def parse_streaming_url(url: str) -> str:
    if "spotify.com" in url:
        try:
            oembed_url = f"https://open.spotify.com/oembed?url={urllib.parse.quote(url)}"
            res = requests.get(oembed_url, timeout=5)
            if res.status_code == 200:
                data = res.json()
                title = data.get("title", "")
                artist = data.get("author_name", "")
                if title:
                    return f"{artist} {title} Official Audio".strip()
        except Exception as e:
            print(f"⚠️ 스포티파이 변환 실패: {e}")

    elif "apple.com" in url:
        try:
            parsed = urllib.parse.urlparse(url)
            query_params = urllib.parse.parse_qs(parsed.query)
            track_id = query_params.get('i', [None])[0]
            if track_id:
                itunes_url = f"https://itunes.apple.com/lookup?id={track_id}&country=kr"
                res = requests.get(itunes_url, timeout=5)
                if res.status_code == 200:
                    results = res.json().get("results", [])
                    if results:
                        return f"{results[0].get('artistName', '')} {results[0].get('trackName', '')}".strip()
        except Exception as e:
            print(f"⚠️ 애플뮤직 변환 실패: {e}")

    return url

# ==========================================
# 💬 채팅 감지 및 메세지 처리 이벤트
# ==========================================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    guild_id = message.guild.id
    user_id = message.author.id

    # 🎧 1. [전용 음악 신청 채널] 감지
    target_channel_id = music_channel_ids.get(guild_id)
    if target_channel_id and message.channel.id == target_channel_id:
        try:
            await message.delete()
        except discord.Forbidden:
            pass

        if not message.author.voice or not message.author.voice.channel:
            warning_msg = await message.channel.send(f"❌ {message.author.mention}님, 먼저 음성 채널에 접속해 주십시오.")
            await asyncio.sleep(3)
            await warning_msg.delete()
            return

        search_term = message.content.strip()
        if not search_term:
            return

        voice_channel = message.author.voice.channel
        voice_client = message.guild.voice_client

        if voice_client is None:
            voice_client = await voice_channel.connect()
        elif voice_client.channel != voice_channel:
            await voice_client.move_to(voice_channel)

        search_term = parse_streaming_url(search_term)
        if not search_term.startswith(('http://', 'https://')):
            search_term = f"ytsearch1:{search_term}"

        try:
            info = await asyncio.to_thread(ytdl.extract_info, search_term, download=False)
            if 'entries' in info and len(info['entries']) > 0:
                info = info['entries'][0]

            url = info.get('webpage_url', f"https://www.youtube.com/watch?v={info.get('id')}")
            title = info.get('title', '음악')
            duration = info.get('duration', 0)
            thumbnail = info.get('thumbnail')

            mins, secs = divmod(duration, 60)
            time_str = f"{mins}분 {secs}초" if mins else f"{secs}초"

            song = {
                'title': title,
                'url': url,
                'webpage_url': url,
                'duration': time_str,
                'thumbnail': thumbnail,
                'requester': message.author
            }
        except Exception as e:
            err_msg = await message.channel.send(f"❌ 음원을 불러오는 중 오류가 발생했습니다: {e}")
            await asyncio.sleep(3)
            await err_msg.delete()
            return

        mdata = get_music_data(guild_id)
        mdata["channel"] = message.channel

        if voice_client.is_playing() or voice_client.is_paused():
            mdata["queue"].append(song)
            await send_or_update_player_embed(message.guild, message.channel)
        else:
            mdata["queue"].append(song)
            await play_next(message.guild, message.channel)

        return

    # 2. 비속어 필터링
    forbidden_words = ["니엄마", "애미", "니애미"]
    if any(word in message.content for word in forbidden_words):
        try:
            await message.author.timeout(discord.utils.utcnow() + timedelta(seconds=60), reason="부적절한 언어 사용")
            embed = discord.Embed(
                title="🚨 경고: 부적절한 언어 감지", 
                description=f"{message.author.mention}님, 규정 위반 언어가 감지되어 60초간 대화를 제한합니다.", 
                color=0xE74C3C
            )
            await message.channel.send(embed=embed)
            await message.delete()
        except Exception:
            pass
        return

    # 3. 경험치/포인트 및 레벨업 시스템
    check_user_data(user_id)
    user_bank[user_id]["weekly_chat_count"] = user_bank[user_id].get("weekly_chat_count", 0) + 1
    user_bank[user_id]["exp"] += random.randint(1, 3)
    user_bank[user_id]["points"] += random.randint(1, 3)

    current_level = user_bank[user_id]["level"]
    required_exp = int(100 * (1.5 ** (current_level - 1)))

    if user_bank[user_id]["exp"] >= required_exp:
        user_bank[user_id]["level"] += 1
        user_bank[user_id]["exp"] = 0
        
        embed = discord.Embed(
            title="📈 [직급/레벨 상승 보고]", 
            description=f"**{message.author.name}**님의 레벨이 상승했습니다.", 
            color=0x3498DB, 
            timestamp=discord.utils.utcnow()
        )
        embed.set_thumbnail(url=message.author.display_avatar.url)
        embed.add_field(name="이전 레벨", value=f"Lv.{current_level}", inline=True)
        embed.add_field(name="현재 레벨", value=f"**Lv.{user_bank[user_id]['level']}**", inline=True)
        embed.set_footer(text="성실한 활동에 감사드립니다.", icon_url=bot.user.display_avatar.url)
        
        # 💡 [통합 채널 설정에서 레벨업 알림 채널 ID 안전하게 가져오기]
        guild_config = server_channels.get(guild_id, {})
        target_channel_id = guild_config.get("levelup")
        target_channel = bot.get_channel(target_channel_id) if target_channel_id else message.channel

        if target_channel:
            await target_channel.send(embed=embed)

    save_data()

    # 4. 단순 응답
    responses = {
        "핑": "퐁. 시스템 응답 속도는 정상입니다.", 
        "인사해": "안녕하십니까. 비서 후후입니다.", 
        "안녕하세요": "안녕하십니까.", 
        "ㅎㅇ": "안녕하십니까."
    }
    if message.content in responses:
        await message.channel.send(responses[message.content])

    # 5. AI 멘션 호출
    if bot.user.mentioned_in(message):
        user_prompt = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
        user_id_str = str(user_id)
        
        if not user_prompt:
            await message.channel.send(f"{message.author.mention} 무슨 일이십니까. 용건을 말씀해 주십시오.")
        else:
            async with message.channel.typing():
                if user_id_str in user_personas:
                    current_persona = user_personas[user_id_str]
                else:
                    current_persona = AI_INSTRUCTION_DEFAULT + get_affinity_persona(user_id)

                response_text = await call_gemini(user_prompt, system_instruction=current_persona)

                if response_text:
                    await message.channel.send(f"{message.author.mention} {response_text}")
                else:
                    await message.channel.send(f"{message.author.mention} 현재 응답 서버가 지연되고 있습니다. 잠시 후 다시 호출해 주십시오.")

    # 6. 명령어 처리 (단 1회 수행)
    await bot.process_commands(message)

# ==========================================
# 🎮 슬래시 명령어 (Slash Commands)
# ==========================================

@bot.tree.command(name="운세", description="Gemini AI가 오늘의 운세를 정성껏 분석하여 보고합니다.")
async def fortune(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        prompt = (
            f"오늘 '{interaction.user.display_name}' 님의 '오늘의 운세'를 아주 상세하고 정성껏 보고서 형식으로 점쳐줘.\n\n"
            "작성 조건:\n"
            "1. 전체적인 총운, 재물운, 연애/인간관계운, 주의사항을 항목별로 정리할 것.\n"
            "2. '오늘의 행운 요소'(행운의 색, 숫자, 아이템)를 명시할 것.\n"
            "3. 어조: 단정하고 차분한 여비서 어조로 작성할 것."
        )
        ai_response = await call_gemini(prompt, system_instruction=AI_INSTRUCTION_DEFAULT)
        if not ai_response:
            ai_response = "🔮 현재 기운 분석에 실패하였습니다. 잠시 후 다시 시도해 주십시오."

        embed = discord.Embed(
            title=f"📊 {interaction.user.display_name}님의 오늘의 운세 보고서",
            description=ai_response,
            color=0x2B2D31,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text="오늘 하루도 원활한 일정이 되시길 바랍니다.", icon_url=bot.user.display_avatar.url)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ 분석 중 오류가 발생했습니다: {e}")

@bot.tree.command(name="요약", description="현재 채널의 최근 일반 대화를 요약 보고합니다.")
async def summarize(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        messages = []
        async for msg in interaction.channel.history(limit=100):
            if msg.author.bot or msg.content.startswith("/") or bot.user.mentioned_in(msg) or not msg.content.strip():
                continue
            messages.append(f"{msg.author.display_name}: {msg.content}")
            if len(messages) >= 30:
                break

        if not messages:
            return await interaction.followup.send("⚠️ 요약할 최근 유저 대화 기록이 없습니다.")

        messages.reverse()
        chat_logs = "\n".join(messages)

        prompt = (
            "너는 전달받은 [대화 기록]만을 객관적으로 요약하는 여비서야.\n"
            f"[대화 기록]\n{chat_logs}\n\n"
            "작성 조건:\n"
            "1. 주요 대화 내용과 흐름을 3~5줄로 간결하게 요약할 것.\n"
            "2. 어조: 무뚝뚝하고 깔끔한 여비서 어조(~습니다, ~입니다)를 유지할 것."
        )

        summary = await call_gemini(prompt, system_instruction=AI_INSTRUCTION_DEFAULT)
        if not summary:
            summary = "📝 대화 내용을 요약하는 데 실패했습니다."

        embed = discord.Embed(
            title=f"📝 #{interaction.channel.name} 채널 업무 요약 보고",
            description=summary,
            color=0x3498DB,
            timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"수집된 대화 수: {len(messages)}개", icon_url=bot.user.display_avatar.url)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ 요약 처리 중 오류가 발생했습니다: {e}")

@bot.tree.command(name="지갑", description="현재 자산 상태를 확인합니다.")
async def money_status(interaction: discord.Interaction):
    user = interaction.user
    check_user_data(user.id)
    balance = user_bank[user.id]['money']
    
    embed = discord.Embed(title="💳 개인 자산 현황 보고", color=0x2B2D31)
    embed.set_author(name=f"{user.name}님의 계좌", icon_url=user.display_avatar.url)
    embed.add_field(name="💵 보유 현금", value=f"**` {balance:,} 원 `**", inline=False)
    embed.set_footer(text="계획적인 자산 관리를 권장합니다.", icon_url=bot.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="월급", description="일일 지급되는 보조금을 신청합니다.")
async def daily_money(interaction: discord.Interaction):
    user = interaction.user
    check_user_data(user.id)
    today = datetime.now().strftime("%Y-%m-%d")
    
    if user_bank[user.id].get("last_date") == today: 
        embed = discord.Embed(title="🚫 일일 보조금 지급 불가", description="오늘 이미 보조금이 지급되었습니다.", color=0xE74C3C)
        embed.add_field(name="📅 재신청 가능 시간", value="내일 자정 이후", inline=True)
        embed.add_field(name="🏦 현재 잔고", value=f"**{user_bank[user.id]['money']:,}원**", inline=True)
        embed.set_footer(text="규정상 하루에 한 번만 지급 가능합니다.", icon_url=user.display_avatar.url)
        await interaction.response.send_message(embed=embed)
    else:
        user_bank[user.id]["money"] += 10000
        user_bank[user.id]["last_date"] = today
        save_data()
        embed = discord.Embed(title="💸 일일 보조금 지급 완료", description="지정 계좌로 지원금이 입금되었습니다.", color=0x2ECC71)
        embed.add_field(name="💰 입금액", value="**+10,000원**", inline=True)
        embed.add_field(name="🏦 변경 후 잔고", value=f"**{user_bank[user.id]['money']:,}원**", inline=True)
        embed.set_footer(text="업무에 유용하게 활용해 주십시오.", icon_url=bot.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

@bot.tree.command(name="도박", description="위험 자산 투자(도박)를 진행합니다.")
@app_commands.describe(amount="투자할 금액 또는 '올인'")
@app_commands.rename(amount="금액")
async def gamble(interaction: discord.Interaction, amount: str):
    user = interaction.user
    check_user_data(user.id)
    balance = user_bank[user.id]["money"]

    if amount == "올인": 
        bet = balance
    else:
        try: 
            bet = int(amount)
        except ValueError: 
            return await interaction.response.send_message("❌ 금액은 정수 또는 '올인'으로 입력해 주십시오.", ephemeral=True)

    if bet <= 0: 
        return await interaction.response.send_message("❌ 0원 이하의 금액은 지정할 수 없습니다.", ephemeral=True)
    if balance < bet: 
        return await interaction.response.send_message(f"❌ 잔액이 부족합니다. (현재 잔액: {balance:,}원)", ephemeral=True)

    loading_embed = discord.Embed(
        title="🎰 투자 결과 처리 중...", 
        description="승률을 계산하고 있습니다. 잠시만 기다려 주십시오.", 
        color=0xF1C40F
    )
    await interaction.response.send_message(embed=loading_embed)
    await asyncio.sleep(2)
    
    win_rate = random.randint(30, 70) 
    is_win = random.random() < (win_rate / 100)
    
    if is_win:
        user_bank[user.id]["money"] += bet
        embed = discord.Embed(title="📈 투자 성공 (이익 발생)", description=f"투자가 성공적으로 마무리되었습니다.\n**+{bet:,}원**의 수익을 창출했습니다.", color=0x2ECC71)
        # 💡 [오타 수정] hhttps:// -> https://
        embed.set_image(url="https://i.pinimg.com/1200x/6a/7a/10/6a7a10af28be6a03266556d3681b8afe.jpg")
    else:
        user_bank[user.id]["money"] -= bet
        embed = discord.Embed(title="📉 투자 실패 (손실 발생)", description=f"투자 자산 집행에 실패하였습니다.\n**-{bet:,}원**의 손실이 발생했습니다.", color=0xE74C3C)
        embed.set_image(url="https://i.pinimg.com/736x/41/ef/c0/41efc031da9ff18cdd557c7e5ac57ec6.jpg")
    
    save_data()
    embed.add_field(name="📊 계산 승률", value=f"` {win_rate}% `", inline=True)
    embed.add_field(name="🏦 현재 잔고", value=f"**` {user_bank[user.id]['money']:,} 원 `**", inline=True)
    embed.set_footer(text=f"신청자: {user.name}", icon_url=user.display_avatar.url)
    
    await interaction.edit_original_response(embed=embed)

@bot.tree.command(name="내레벨", description="현재 레벨, 착용 칭호 및 활동 프로필을 조회합니다.")
async def level_info(interaction: discord.Interaction):
    user = interaction.user
    check_title_achievements(user.id)
    save_data()
    
    u_data = user_bank[user.id]
    current_level = u_data['level']
    current_exp = u_data['exp']
    equipped_title = u_data.get('equipped_title', '🌱 신입 사원')
    required_exp = int(100 * (1.5 ** (current_level - 1)))
    
    progress = int((current_exp / required_exp) * 10) if required_exp > 0 else 10
    bar = "🟦" * progress + "⬜" * (10 - progress)

    embed = discord.Embed(title="📊 사원 프로필 및 활동 조회", color=0x3498DB)
    embed.set_thumbnail(url=user.display_avatar.url)
    
    embed.add_field(name="👤 사용자", value=f"**[{equipped_title}] {user.name}**", inline=False)
    embed.add_field(name="🚀 현재 레벨", value=f"**Lv. {current_level}**", inline=True)
    embed.add_field(name="💎 보유 포인트", value=f"**{u_data['points']:,} P**", inline=True)
    embed.add_field(name="🏦 보유 자산", value=f"**{u_data['money']:,} 원**", inline=True)
    embed.add_field(name=f"✨ 경험치 달성도 ({current_exp}/{required_exp})", value=bar, inline=False)
    
    embed.set_footer(text="`/칭호` 명령어로 착용 중인 칭호를 변경할 수 있습니다.", icon_url=bot.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="레벨랭킹", description="서버 레벨 순위를 조회합니다.")
async def level_ranking(interaction: discord.Interaction):
    await interaction.response.defer()
    ranking = sorted(
        [(await bot.fetch_user(k), v['level'], v['exp']) for k, v in user_bank.items()], 
        key=lambda x: (x[1], x[2]), 
        reverse=True
    )[:5]
    
    embed = discord.Embed(title="🏆 서버 레벨 순위 (상위 5명)", description="서버 내 최고 레벨 보유자 명단입니다.", color=0xFFA500)
    medals = ["🥇", "🥈", "🥉", "🏅", "🏅"]
    for i, (user_obj, lvl, exp) in enumerate(ranking):
        embed.add_field(name=f"{medals[i]} {i+1}위: {user_obj.name}", value=f"**Lv.{lvl}** (경험치: {exp})", inline=False)
        
    embed.set_footer(text="이상 상위 활동자 보고였습니다.", icon_url=bot.user.display_avatar.url)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="도박랭킹", description="서버 자산 순위를 조회합니다.")
async def money_ranking(interaction: discord.Interaction):
    await interaction.response.defer()
    ranking = sorted(
        [(await bot.fetch_user(k), v['money']) for k, v in user_bank.items()], 
        key=lambda x: x[1], 
        reverse=True
    )[:5]
    
    embed = discord.Embed(title="💰 최고 자산가 순위 (상위 5명)", description="현재 서버 내 가장 높은 자산을 보유한 명단입니다.", color=0xFFD700)
    medals = ["🥇", "🥈", "🥉", "🏅", "🏅"]
    for i, (user_obj, money) in enumerate(ranking):
        embed.add_field(name=f"{medals[i]} {i+1}위: {user_obj.name}", value=f"보유 자산: **` {money:,} 원 `**", inline=False)
        
    embed.set_footer(text="이상 자산 보유 상위 보고였습니다.", icon_url=bot.user.display_avatar.url)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="청소", description="지정한 개수만큼 채팅을 청소합니다. (메시지 관리 권한 필요)")
@app_commands.describe(amount="삭제할 메시지 개수 (1 ~ 100)")
@app_commands.checks.has_permissions(manage_messages=True)
async def clear_chat(interaction: discord.Interaction, amount: int):
    if not (1 <= amount <= 100):
        return await interaction.response.send_message("❌ 청소할 메시지는 `1`개에서 `100`개 사이로 지정해 주십시오.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"🧹 성공적으로 **`{len(deleted)}개`**의 메시지를 청소했습니다.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ **봇의 권한이 부족합니다!** 디스코드 서버 설정에서 봇에게 **'메시지 관리'** 권한을 부여해 주십시오.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 청소 작업 중 오류가 발생했습니다: {e}", ephemeral=True)

@clear_chat.error
async def clear_chat_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ 이 명령어를 사용할 수 있는 **'메시지 관리'** 권한이 없습니다.", ephemeral=True)

@bot.tree.command(name="돈지급", description="관리자 권한으로 자금을 지급합니다.")
@app_commands.describe(member="지급 대상 유저", amount="지급 액수")
@app_commands.rename(member="유저", amount="액수")
@app_commands.checks.has_permissions(administrator=True)
async def give_money(interaction: discord.Interaction, member: discord.Member, amount: int):
    check_user_data(member.id)
    user_bank[member.id]["money"] += amount
    save_data()
    
    embed = discord.Embed(title="💰 특별 자금 지급 완료", description=f"{member.mention}님에게 **{amount:,}원**의 지원금이 집행되었습니다.", color=0x2ECC71)
    embed.add_field(name="🏦 변경된 잔고", value=f"**{user_bank[member.id]['money']:,}원**")
    embed.set_footer(text="지급 내역이 기록되었습니다.", icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="포인트지급", description="[관리자] 특정 유저에게 포인트를 지급합니다.")
@app_commands.describe(target="포인트를 지급할 유저", amount="지급할 포인트")
@app_commands.rename(target="대상", amount="포인트")
@app_commands.checks.has_permissions(administrator=True)
async def give_points(interaction: discord.Interaction, target: discord.Member, amount: int):
    if target.bot:
        return await interaction.response.send_message("❌ AI 보조 계정에는 포인트를 지급할 수 없습니다.", ephemeral=True)
        
    check_user_data(target.id)
    user_bank[target.id]["points"] += amount
    save_data()
    
    embed = discord.Embed(title="💎 업무 보너스 포인트 지급 완료", description=f"{target.mention}님에게 **{amount:,} P**가 지급되었습니다.", color=0x3498DB)
    embed.add_field(name="✨ 변경된 포인트", value=f"**{user_bank[target.id]['points']:,} P**")
    embed.set_footer(text="지급 내역이 기록되었습니다.", icon_url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="도움말", description="여비서 후후의 기능 및 매뉴얼을 확인합니다.")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="📋 여비서 후후 - 시스템 업무 매뉴얼", description="제공되는 기능 및 명령어 목록입니다.", color=0x2B2D31)
    embed.add_field(name="🤖 1. 대화 지원", value="• **봇 태그 호출** (`@후후 [내용]`)\n  - 여비서 AI와 1:1 대화를 진행합니다.", inline=False)
    embed.add_field(
        name="💼 2. 시스템 및 보조 기능", 
        value="• `/지갑` / `/월급` / `/도박` / `/도박랭킹` / `/레벨랭킹`\n• `/운세` / `/요약` / `/상점` / `/저격 [@유저]`", 
        inline=False
    )
    embed.add_field(
        name="📈 3. 프로필 및 데이터 관리", 
        value="• `/내레벨` / `/호감도` / `/선물 [포인트]` / `/사원증` / `/칭호` / `/채널설정`", 
        inline=False
    )
    embed.set_footer(text="필요한 업무가 있으시면 언제든 명령어를 입력해 주십시오.", icon_url=bot.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="상점", description="포인트를 활용하여 장비 및 개인 역할권을 구매합니다.")
@app_commands.describe(action="조회 또는 구매 선택", item_count="구매 수량 (저격 소총 전용)")
@app_commands.rename(action="선택", item_count="수량")
@app_commands.choices(action=[
    app_commands.Choice(name="🛒 보유 아이템 및 목록 조회", value="view"),
    app_commands.Choice(name="💳 저격 소총 구매 (500 P)", value="buy_sniper"),
    app_commands.Choice(name="🎨 개인 역할권 구매 (5,000 P)", value="buy_role")
])
async def shop_command(interaction: discord.Interaction, action: str = "view", item_count: int = 1):
    user_id = interaction.user.id
    check_inventory(user_id)
    
    if action == "view":
        embed = discord.Embed(title="🛒 특수 장비 및 권한 상점", description="포인트를 활용하여 제재 장비 및 개인 역할권을 구입할 수 있습니다.", color=0x2B2D31)
        embed.add_field(
            name="🏹 [특수 장비] 저격 소총", 
            value=f"• **가격:** ` 500 P `\n• **효과:** 지정 유저 1명을 60초간 음소거 조치\n• **보유 수량:** ` {user_bank[user_id]['inventory'].get('sniper_rifle', 0)}개 `", 
            inline=False
        )
        embed.add_field(
            name="🎨 [특수 권한] 개인 역할권", 
            value=f"• **가격:** ` 5,000 P `\n• **효과:** 나만의 고유 역할 생성 및 부여\n• **보유 수량:** ` {user_bank[user_id]['inventory'].get('custom_role_ticket', 0)}개 `", 
            inline=False
        )
        embed.add_field(name="💎 보유 포인트", value=f"**` {user_bank[user_id]['points']:,} P `**", inline=False)
        embed.set_footer(text="구매 승인 후 환불은 불가합니다.", icon_url=bot.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    elif action == "buy_sniper":
        if item_count <= 0:
            return await interaction.response.send_message("❌ 최소 1개 이상 구매해 주십시오.", ephemeral=True)
            
        total_price = 500 * item_count
        if user_bank[user_id]["points"] < total_price:
            return await interaction.response.send_message(f"❌ 포인트가 부족합니다. (필요: {total_price:,} P)", ephemeral=True)
            
        user_bank[user_id]["points"] -= total_price
        user_bank[user_id]["inventory"]["sniper_rifle"] += item_count
        save_data()
        
        embed = discord.Embed(title="📦 장비 결제 승인 완료", description=f"**저격 소총 {item_count}개** 수령이 완료되었습니다.", color=0x2ECC71)
        embed.add_field(name="💰 차감 포인트", value=f"-{total_price:,} P", inline=True)
        embed.add_field(name="🏹 총 보유 수량", value=f"{user_bank[user_id]['inventory']['sniper_rifle']}개", inline=True)
        embed.set_footer(text="안전에 유의하여 사용해 주십시오.", icon_url=bot.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    elif action == "buy_role":
        price = 5000
        if user_bank[user_id].get("has_purchased_role", False):
            return await interaction.response.send_message("❌ 개인 역할권은 계정당 1회만 구매할 수 있습니다.", ephemeral=True)
        
        if user_bank[user_id]["points"] < price:
            return await interaction.response.send_message(f"❌ 포인트가 부족합니다. (필요: {price:,} P)", ephemeral=True)
            
        user_bank[user_id]["points"] -= price
        user_bank[user_id]["inventory"]["custom_role_ticket"] = user_bank[user_id]["inventory"].get("custom_role_ticket", 0) + 1
        user_bank[user_id]["has_purchased_role"] = True
        save_data()
        
        embed = discord.Embed(title="🎨 개인 역할권 결제 완료", description="개인 역할권 1개 구매 승인이 완료되었습니다.\n`/개인역할생성` 명령어로 역할을 만드실 수 있습니다.", color=0x9B59B6)
        embed.add_field(name="💰 차감 포인트", value=f"-{price:,} P", inline=True)
        embed.add_field(name="🎫 보유 역할권 수량", value=f"{user_bank[user_id]['inventory']['custom_role_ticket']}개", inline=True)
        embed.set_footer(text="※ 개인 역할권은 계정당 1회만 구매 가능합니다.", icon_url=bot.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)

@bot.tree.command(name="저격", description="[장비 필요] 지정한 대상을 60초간 음소거 조치합니다.")
@app_commands.describe(target="제재할 유저")
@app_commands.rename(target="대상")
async def snipe_user(interaction: discord.Interaction, target: discord.Member):
    user_id = interaction.user.id
    check_inventory(user_id)
    
    if user_bank[user_id]["inventory"]["sniper_rifle"] <= 0:
        return await interaction.response.send_message("❌ 보유 중인 저격 소총이 없습니다. `/상점`에서 구매해 주십시오.", ephemeral=True)
        
    if target.bot or target.id == user_id:
        return await interaction.response.send_message("❌ 대상을 지정할 수 없습니다.", ephemeral=True)

    aim_embed = discord.Embed(title="🎯 제재 조치 진행 중...", description=f"{target.mention}님에 대한 조준을 진행합니다.", color=0xE67E22)
    aim_embed.set_image(url="https://search.pstatic.net/sunny/?src=https%3A%2F%2Fi.namu.wiki%2Fi%2FfZJhe9aQaPdJx821dw8i6N9BWM577ve7tjBLYx_K36gR6gncMpnmLhpU4mIVgRL0gXGm7keBbBpJG8rRz2bIag.gif&type=sc960_832_gif")
    await interaction.response.send_message(embed=aim_embed)
    await asyncio.sleep(2)
    
    try:
        await target.timeout(discord.utils.utcnow() + timedelta(seconds=60), reason=f"{interaction.user.name}의 저격 장비 집행")
        user_bank[user_id]["inventory"]["sniper_rifle"] -= 1
        save_data()
        
        hit_embed = discord.Embed(title="💥 제재 집행 완료", description=f"{target.mention}님을 **60초 동안 음소거(타임아웃)** 조치하였습니다.", color=0xE74C3C)
        hit_embed.set_image(url="https://i.pinimg.com/1200x/ae/16/f0/ae16f0fb57f807e8bccb32ee480ade14.jpg")
        hit_embed.set_footer(text=f"잔여 저격 소총: {user_bank[user_id]['inventory']['sniper_rifle']}개", icon_url=interaction.user.display_avatar.url)
        await interaction.edit_original_response(embed=hit_embed)
    except Exception:
        await interaction.edit_original_response(content="❌ **집행 실패:** 대상의 권한이 너무 높거나 권한 제한으로 인해 집행할 수 없습니다. (장비 소모 없음)", embed=None)

@bot.tree.command(name="호감도", description="여비서와의 업무 신뢰 지수를 확인합니다.")
@app_commands.describe(target="조회할 유저")
@app_commands.rename(target="대상")
async def check_affinity(interaction: discord.Interaction, target: discord.Member = None):
    target_user = target or interaction.user
    user_id = target_user.id
    check_user_data(user_id)
    
    affinity = user_bank[user_id]["affinity"]
    bar = get_affinity_bar(affinity)
    status = "💼 전속 보좌 (최상)" if affinity >= 85 else ("🤝 원활 (양호)" if affinity >= 60 else ("📋 보통" if affinity >= 35 else "❄️ 경계 (낮음)"))
    
    embed = discord.Embed(
        title=f"📊 {target_user.display_name}님과 여비서의 업무 신뢰 지수", 
        description=f"**관계 상태:** `{status}`\n\n`{bar}` **{affinity} / 100 P**", 
        color=0x2B2D31
    )
    embed.set_thumbnail(url=target_user.display_avatar.url)
    embed.set_footer(text="지속적인 관리를 권장합니다.", icon_url=bot.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="선물", description="여비서에게 포인트를 제출하여 신뢰 지수를 높입니다.")
@app_commands.describe(amount="제출할 포인트")
@app_commands.rename(amount="포인트")
async def give_gift(interaction: discord.Interaction, amount: int):
    user_id = interaction.user.id
    check_user_data(user_id)
    current_affinity = user_bank[user_id]["affinity"]
    
    if current_affinity >= 100:
        return await interaction.response.send_message("💼 이미 최고 신뢰 단계(100 P)입니다.", ephemeral=True)
        
    pts_per_affinity = 3000 if current_affinity >= 80 else (2000 if current_affinity >= 50 else 1000)

    if amount < pts_per_affinity:
        return await interaction.response.send_message(f"❌ 최소 **{pts_per_affinity:,} P** 이상 제출해야 합니다.", ephemeral=True)
        
    if user_bank[user_id]["points"] < amount:
        return await interaction.response.send_message(f"❌ 보유 중인 포인트가 부족합니다.", ephemeral=True)
        
    added_affinity = amount // pts_per_affinity
    new_affinity = min(100, current_affinity + added_affinity)
    real_added = new_affinity - current_affinity
    actual_used_points = real_added * pts_per_affinity
    
    user_bank[user_id]["points"] -= actual_used_points
    user_bank[user_id]["affinity"] = new_affinity
    save_data()
    
    embed = discord.Embed(title="🎁 포인트 제출 처리 승인", description=f"신뢰 지수가 **+{real_added} P** 증가했습니다.", color=0x2B2D31)
    embed.add_field(name="📊 현재 신뢰 지수", value=f"**{new_affinity} / 100 P**", inline=True)
    embed.add_field(name="💎 남은 포인트", value=f"**{user_bank[user_id]['points']:,} P**", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="내정보초기화", description="본인의 모든 자산, 레벨, 신뢰도 데이터를 초기화합니다.")
async def reset_my_data(interaction: discord.Interaction):
    user_id = interaction.user.id
    user_bank[user_id] = {
        "money": 10000, "last_date": "", "exp": 0, "level": 1, "points": 0, 
        "inventory": {"sniper_rifle": 0}, "affinity": 50
    }
    save_data()
    await interaction.response.send_message("⚠️ 개인 데이터 초기화가 완료되었습니다.", ephemeral=True)

@bot.tree.command(name="데이터초기화", description="[관리자 권한] 지정 유저 또는 전체 유저 데이터를 초기화합니다.")
@app_commands.describe(scope="초기화 범위 선택", target="초기화할 특정 유저")
@app_commands.rename(scope="범위", target="대상")
@app_commands.choices(scope=[
    app_commands.Choice(name="👤 지정한 유저 1명 초기화", value="user"),
    app_commands.Choice(name="🚨 데이터베이스 전체 초기화 (모든 유저)", value="all")
])
@app_commands.checks.has_permissions(administrator=True)
async def reset_server_data(interaction: discord.Interaction, scope: str, target: discord.Member = None):
    global user_bank
    if scope == "user":
        if not target:
            return await interaction.response.send_message("❌ 대상을 지정해야 합니다.", ephemeral=True)
        user_bank[target.id] = {
            "money": 10000, "last_date": "", "exp": 0, "level": 1, "points": 0, 
            "inventory": {"sniper_rifle": 0}, "affinity": 50
        }
        save_data()
        await interaction.response.send_message(f"🧹 {target.mention}님의 데이터가 초기화되었습니다.")
    elif scope == "all":
        user_bank.clear()
        save_data()
        await interaction.response.send_message("🚨 전체 데이터베이스가 초기화되었습니다.")

@bot.tree.command(name="채널설정", description="특수 안내 및 전용 채널을 지정합니다. (관리자 권한)")
@app_commands.describe(category="설정 항목", channel="지정 채널")
@app_commands.rename(category="항목", channel="채널")
@app_commands.choices(category=[
    app_commands.Choice(name="🎵 매일 음악 추천 보고 채널", value="music"),
    app_commands.Choice(name="🆙 레벨업 알림 지정 채널", value="levelup"),
    app_commands.Choice(name="📊 주간 서버 결산 보고 채널", value="briefing"),
    app_commands.Choice(name="🎧 전용 음악 신청 채널", value="player")
])
async def set_special_channel(
    interaction: discord.Interaction, 
    category: app_commands.Choice[str], 
    channel: discord.TextChannel
):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ 이 명령어는 서버 관리자만 사용할 수 있습니다.", ephemeral=True)

    guild_id = interaction.guild_id
    cat_val = category.value

    if guild_id not in server_channels:
        server_channels[guild_id] = {}

    server_channels[guild_id][cat_val] = channel.id
    save_channel_configs(server_channels)

    if cat_val == "player":
        await interaction.response.defer(ephemeral=True)
        music_channel_ids[guild_id] = channel.id

        mdata = get_music_data(guild_id)
        mdata["channel"] = channel

        try:
            await channel.purge(limit=100)
        except discord.Forbidden:
            return await interaction.followup.send("❌ 메시지 삭제 권한이 없습니다.", ephemeral=True)
        except Exception as e:
            print(f"⚠️ 채널 청소 중 예외: {e}")

        await send_or_update_player_embed(interaction.guild, channel)
        await interaction.followup.send(f"✅ {channel.mention} 채널이 **전용 음악 신청 채널**로 설정되었습니다!", ephemeral=True)

    elif cat_val == "music":
        server_config["music_channel_id"] = channel.id
        save_config()
        await interaction.response.send_message(f"🎵 {channel.mention} 채널이 **매일 아침 음악 추천 보고 채널**로 지정되었습니다.", ephemeral=True)

    elif cat_val == "levelup":
        await interaction.response.send_message(f"🆙 {channel.mention} 채널이 **레벨업 알림 채널**로 지정되었습니다.", ephemeral=True)

    elif cat_val == "briefing":
        server_config["briefing_channel_id"] = channel.id
        save_config()
        await interaction.response.send_message(f"📊 {channel.mention} 채널이 **주간 서버 결산 보고 채널**로 지정되었습니다.", ephemeral=True)

@bot.tree.command(name="성격설정", description="[관리자] 특정 유저 전용 AI 성격을 지정하거나 변경합니다.")
@app_commands.describe(target="성격을 적용할 유저", prompt="AI에게 지시할 성격/페르소나 지침")
@app_commands.rename(target="대상", prompt="지침내용")
@app_commands.checks.has_permissions(administrator=True)
async def set_persona(interaction: discord.Interaction, target: discord.Member, prompt: str):
    user_personas[str(target.id)] = prompt
    save_personas()
    await interaction.response.send_message(f"🎭 {target.mention} 님의 전용 AI 성격이 설정되었습니다.")

@bot.tree.command(name="성격초기화", description="[관리자] 특정 유저의 전용 AI 성격을 삭제합니다.")
@app_commands.describe(target="초기화할 유저")
@app_commands.rename(target="대상")
@app_commands.checks.has_permissions(administrator=True)
async def reset_persona(interaction: discord.Interaction, target: discord.Member):
    user_id_str = str(target.id)
    if user_id_str in user_personas:
        del user_personas[user_id_str]
        save_personas()
        await interaction.response.send_message(f"✅ {target.mention} 님의 전용 AI 성격이 삭제되었습니다.")
    else:
        await interaction.response.send_message("❌ 설정된 전용 성격이 없습니다.", ephemeral=True)

@bot.tree.command(name="성격목록", description="[관리자] 현재 등록된 유저별 전용 AI 성격 목록을 확인합니다.")
@app_commands.checks.has_permissions(administrator=True)
async def list_personas(interaction: discord.Interaction):
    if not user_personas:
        return await interaction.response.send_message("ℹ️ 등록된 전용 성격이 없습니다.", ephemeral=True)

    embed = discord.Embed(title="🎭 등록된 유저별 AI 성격 목록", color=0x9B59B6)
    for u_id_str, prompt in user_personas.items():
        member = interaction.guild.get_member(int(u_id_str))
        user_display = member.mention if member else f"ID: {u_id_str}"
        embed.add_field(name=f"👤 {user_display}", value=f"```\n{prompt[:100]}\n```", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="개인역할생성", description="[보유 필요] 개인 역할권을 사용하여 나만의 역할을 생성합니다.")
@app_commands.describe(name="생성할 역할 이름", color="Hex 색상 코드 (예: #FF5733)")
@app_commands.rename(name="역할이름", color="색상코드")
async def create_custom_role(interaction: discord.Interaction, name: str, color: str):
    user_id = interaction.user.id
    check_inventory(user_id)

    if user_bank[user_id]["inventory"].get("custom_role_ticket", 0) <= 0:
        return await interaction.response.send_message("❌ 보유 중인 개인 역할권이 없습니다.", ephemeral=True)

    raw_color = color.strip().lstrip("#")
    try:
        color_int = int(raw_color, 16)
        discord_color = discord.Color(color_int)
    except ValueError:
        return await interaction.response.send_message("❌ 올바르지 않은 Hex 색상 코드입니다.", ephemeral=True)

    await interaction.response.defer()
    guild = interaction.guild
    try:
        new_role = await guild.create_role(name=name, color=discord_color, reason="개인 역할권 사용")
        anchor_role = discord.utils.get(guild.roles, name="--- 개인 역할 ---")
        
        if anchor_role:
            target_position = max(1, anchor_role.position - 1)
            await new_role.edit(position=target_position)
        else:
            bot_member = guild.get_member(bot.user.id)
            target_position = max(1, bot_member.top_role.position - 1)
            await new_role.edit(position=target_position)

        await interaction.user.add_roles(new_role)
        user_bank[user_id]["inventory"]["custom_role_ticket"] -= 1
        save_data()

        embed = discord.Embed(title="🎨 개인 역할 발급 완료", description=f"{new_role.mention} 이(가) 적용되었습니다.", color=discord_color)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ 역할 생성 중 오류: {e}")

@bot.tree.command(name="칭호", description="칭호 보유 현황, 착용 변경 및 도감을 조회합니다.")
@app_commands.describe(action="조회, 착용 또는 도감 선택", title_name="착용할 칭호 이름")
@app_commands.rename(action="선택", title_name="칭호이름")
@app_commands.choices(action=[
    app_commands.Choice(name="📜 보유 칭호 목록 조회", value="view"),
    app_commands.Choice(name="🎖️ 칭호 착용 변경", value="equip"),
    app_commands.Choice(name="📖 전체 칭호 도감 & 획득 방법", value="info")
])
async def manage_titles(interaction: discord.Interaction, action: str, title_name: typing.Optional[str] = None):
    await interaction.response.defer()
    user_id = interaction.user.id
    check_title_achievements(user_id)
    save_data()
    
    u_data = user_bank[user_id]
    my_titles = u_data.get("titles", ["🌱 신입 사원"])
    current_title = u_data.get("equipped_title", "🌱 신입 사원")

    ALL_TITLES_INFO = [
        ("🌱 신입 사원", "서버 가입 시 기본 제공"),
        ("🚀 베테랑 사원", "레벨 10 달성"),
        ("💰 서버 대부호", "보유 현금 1,000,000원 이상"),
        ("💎 포인트 재벌", "보유 포인트 10,000 P 이상"),
        ("💼 전속 보좌관", "신뢰도 85% 이상"),
        ("🎨 아티스트", "개인 역할권 구매")
    ]

    if action == "view":
        embed = discord.Embed(title=f"🎖️ {interaction.user.name}님의 칭호 보관함", color=0x9B59B6)
        embed.add_field(name="현재 착용 중인 칭호", value=f"**` {current_title} `**", inline=False)
        titles_str = "\n".join([f"• {t}" + (" *(착용 중)*" if t == current_title else "") for t in my_titles])
        embed.add_field(name="보유 칭호 목록", value=titles_str, inline=False)
        await interaction.followup.send(embed=embed)

    elif action == "equip":
        if not title_name or title_name not in my_titles:
            return await interaction.followup.send("❌ 보유 중인 올바른 칭호 이름을 입력해 주십시오.")

        user_bank[user_id]["equipped_title"] = title_name
        save_data()
        await interaction.followup.send(f"✅ 착용 칭호가 **` {title_name} `**(으)로 변경되었습니다.")

    elif action == "info":
        embed = discord.Embed(title="📖 후후 서버 칭호 도감 & 획득 방법", color=0xF1C40F)
        for name, condition in ALL_TITLES_INFO:
            status_tag = "✅ `획득 완료`" if name in my_titles else "🔒 `미획득`"
            embed.add_field(name=f"{name} ({status_tag})", value=f"• **획득 조건:** {condition}", inline=False)
        await interaction.followup.send(embed=embed)

@bot.tree.command(name="사원증", description="후후 서버 공식 사원증을 발급 및 조회합니다.")
@app_commands.describe(target="사원증을 조회할 대상 사원")
@app_commands.rename(target="대상")
async def issue_employee_id(interaction: discord.Interaction, target: typing.Optional[discord.Member] = None):
    await interaction.response.defer()
    member = target or interaction.user
    if member.bot:
        return await interaction.followup.send("❌ 봇 계정은 사원증 발급 대상이 아닙니다.")

    check_title_achievements(member.id)
    save_data()
    
    u_data = user_bank[member.id]
    emp_no = f"HH-{str(member.id)[:8]}"
    join_date_str = member.joined_at.strftime("%Y년 %m월 %d일") if member.joined_at else "정보 없음"

    embed = discord.Embed(
        title="💳 OFFICIAL EMPLOYEE ID CARD",
        description=f"**후후 서버 공식 사원증**\n`사원 번호: {emp_no}`",
        color=0x2C3E50,
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="👤 성명", value=f"**[{u_data.get('equipped_title', '🌱 신입 사원')}] {member.display_name}**", inline=False)
    embed.add_field(name="🎖️ 직급", value=f"`Lv. {u_data.get('level', 1)}`", inline=True)
    embed.add_field(name="📅 입사일", value=f"`{join_date_str}`", inline=True)
    embed.add_field(name="💼 신뢰도", value=f"`❤️ {u_data.get('affinity', 50)}%`", inline=True)
    embed.add_field(name="🏦 보유 자산/포인트", value=f"`{u_data.get('money', 10000):,} 원` | `{u_data.get('points', 0):,} P`", inline=False)
    embed.set_footer(text="후후 비서실 공식 인증", icon_url=bot.user.display_avatar.url)

    await interaction.followup.send(embed=embed)

# ==========================================
# 🎭 드롭다운 역할 선택 메뉴 클래스
# ==========================================
class RoleSelect(discord.ui.Select):
    def __init__(self, roles: list[discord.Role]):
        options = [
            discord.SelectOption(
                label=role.name,
                value=str(role.id),
                description=f"클릭하여 '{role.name}' 역할을 지급/해제합니다.",
                emoji="🏷️"
            ) for role in roles if role is not None
        ]
        super().__init__(placeholder="지급받을 역할을 선택하세요...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        role_id = int(self.values[0])
        role = interaction.guild.get_role(role_id)
        if not role:
            return await interaction.response.send_message("❌ 역할을 찾을 수 없습니다.", ephemeral=True)
            
        user = interaction.user
        if role.position >= interaction.guild.me.top_role.position:
            return await interaction.response.send_message("❌ 봇의 역할 순위가 낮아 부여할 수 없습니다.", ephemeral=True)

        if role in user.roles:
            await user.remove_roles(role)
            await interaction.response.send_message(f"🗑️ **`{role.name}`** 역할이 해제되었습니다.", ephemeral=True)
        else:
            await user.add_roles(role)
            await interaction.response.send_message(f"✅ **`{role.name}`** 역할이 지급되었습니다!", ephemeral=True)

class RoleSelectView(discord.ui.View):
    def __init__(self, roles: list[discord.Role]):
        super().__init__(timeout=None)
        self.add_item(RoleSelect(roles))

@bot.tree.command(name="역할게시판", description="[관리자] 유저들이 스스로 선택할 수 있는 역할 선택 게시판을 생성합니다.")
@app_commands.describe(title="게시판 제목", role1="첫 번째 역할", role2="두 번째 역할", role3="세 번째 역할", role4="네 번째 역할")
@app_commands.default_permissions(administrator=True)
async def create_role_menu(
    interaction: discord.Interaction,
    title: str,
    role1: discord.Role,
    role2: discord.Role = None,
    role3: discord.Role = None,
    role4: discord.Role = None
):
    roles = [r for r in [role1, role2, role3, role4] if r is not None]
    if not roles:
        return await interaction.response.send_message("❌ 최소 1개 이상의 역할을 지정해야 합니다.", ephemeral=True)

    embed = discord.Embed(
        title=f"🎭 {title}",
        description="아래 드롭다운 메뉴를 클릭하여 원하는 역할을 선택해 주십시오.",
        color=0x34495E
    )
    embed.set_footer(text="후후 비서실 자율 역할 시스템", icon_url=bot.user.display_avatar.url)
    await interaction.channel.send(embed=embed, view=RoleSelectView(roles))
    await interaction.response.send_message("✅ 역할 게시판이 성공적으로 생성되었습니다!", ephemeral=True)

# ==========================================
# 🎵 슬래시 음악 명령어
# ==========================================
# 🎵 1. 노래 재생 및 대기열 추가 (/재생)
@bot.tree.command(name="재생", description="음성 채널에 접속하여 음악을 재생하거나 대기열에 추가합니다.")
@app_commands.describe(search="노래 제목 또는 유튜브/애플뮤직/스포티파이 URL")
async def play_music(interaction: discord.Interaction, search: str):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("❌ 먼저 음성 채널에 접속해 주십시오.", ephemeral=True)

    voice_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client

    # 음성 채널 접속 및 이동
    if voice_client is None:
        voice_client = await voice_channel.connect()
    elif voice_client.channel != voice_channel:
        await voice_client.move_to(voice_channel)

    # 💡 애플뮤직/스포티파이 URL 변환 및 유튜브 검색어 포맷팅
    search_term = parse_streaming_url(search.strip())
    if not search_term.startswith(('http://', 'https://')):
        search_term = f"ytsearch1:{search_term}"

    try:
        info = await asyncio.to_thread(ytdl.extract_info, search_term, download=False)
        if 'entries' in info and len(info['entries']) > 0:
            info = info['entries'][0]

        url = info.get('webpage_url', f"https://www.youtube.com/watch?v={info.get('id')}")
        title = info.get('title', '음악')
        duration = info.get('duration', 0)

        mins, secs = divmod(duration, 60)
        time_str = f"{mins}분 {secs}초" if mins else f"{secs}초"

        song = {
            'title': title,
            'url': url,
            'webpage_url': url,
            'duration': time_str,
            'requester': interaction.user
        }
    except Exception as e:
        return await interaction.followup.send(f"❌ 음원을 불러오는 중 오류가 발생했습니다: {e}", ephemeral=True)

    mdata = get_music_data(interaction.guild_id)
    mdata["channel"] = interaction.channel

    if voice_client.is_playing() or voice_client.is_paused():
        mdata["queue"].append(song)
        await interaction.followup.send(f"📥 `{title}` 곡을 대기열에 추가했습니다.", ephemeral=True)
        await send_or_update_player_embed(interaction.guild, interaction.channel)
    else:
        mdata["queue"].append(song)
        await interaction.followup.send(f"🎵 `{title}` 재생을 시작합니다.", ephemeral=True)
        await play_next(interaction.guild, interaction.channel)

@bot.tree.command(name="스킵", description="현재 재생 중인 음악을 건너뛰고 다음 곡을 재생합니다.")
async def skip_music(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await interaction.response.send_message("⏭️ 현재 재생 중인 곡을 건너뛰었습니다.")
    else:
        await interaction.response.send_message("❌ 현재 재생 중인 음악이 없습니다.", ephemeral=True)

@bot.tree.command(name="대기열", description="현재 재생 중인 곡과 남은 대기열 목록을 확인합니다.")
async def show_queue(interaction: discord.Interaction):
    mdata = get_music_data(interaction.guild_id)
    current = mdata.get("current")
    queue = mdata.get("queue", [])

    if not current and not queue:
        return await interaction.response.send_message("📜 현재 대기열이 비어 있습니다.", ephemeral=True)

    embed = discord.Embed(title="📜 [후후 음악실] 재생 대기열 목록", color=0x3498DB)
    if current:
        embed.add_field(name="▶️ 현재 재생 중", value=f"**[{current['title']}]** (`{current['duration']}`) - 요청자: {current['requester'].mention}", inline=False)

    if queue:
        queue_str = ""
        for idx, song in enumerate(queue[:10], start=1):
            queue_str += f"**{idx}.** `{song['title']}` (`{song['duration']}`) | {song['requester'].display_name}\n"
        if len(queue) > 10:
            queue_str += f"\n*...외 {len(queue) - 10}곡 더 남음*"
        embed.add_field(name="📋 다음 대기 곡 목록", value=queue_str, inline=False)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="퇴장", description="음악 재생을 중단하고 대기열을 비운 뒤 퇴장합니다.")
async def leave_voice(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    mdata = get_music_data(interaction.guild_id)
    mdata["queue"].clear()
    mdata["current"] = None

    if vc and vc.is_connected():
        await vc.disconnect()
        await interaction.response.send_message("👋 대기열을 모두 삭제하고 음성 채널에서 퇴장하였습니다.")
    else:
        await interaction.response.send_message("❌ 봇이 접속해 있지 않습니다.", ephemeral=True)

@bot.tree.command(name="일시정지", description="현재 재생 중인 음악을 일시정지합니다.")
async def pause_music(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ 음악을 일시정지했습니다.")
    else:
        await interaction.response.send_message("❌ 현재 재생 중인 음악이 없습니다.", ephemeral=True)

@bot.tree.command(name="다시재생", description="일시정지된 음악을 다시 재생합니다.")
async def resume_music(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ 음악을 다시 재생합니다.")
    else:
        await interaction.response.send_message("❌ 일시정지된 음악이 없습니다.", ephemeral=True)

@bot.tree.command(name="스마트dj", description="대기열이 끝났을 때 AI가 다음 곡을 자동 추천할지 설정합니다.")
async def toggle_smart_dj(interaction: discord.Interaction):
    mdata = get_music_data(interaction.guild_id)
    mdata['smart_dj'] = not mdata.get('smart_dj', False)
    status_str = "🟢 **켜짐 (ON)**" if mdata['smart_dj'] else "🔴 **꺼짐 (OFF)**"
    
    embed = discord.Embed(
        title="🤖 AI 스마트 DJ 설정 변경",
        description=f"AI 스마트 DJ 기능이 {status_str} 상태로 변경되었습니다.",
        color=0x2ECC71 if mdata['smart_dj'] else 0xE74C3C
    )
    await interaction.response.send_message(embed=embed)
    await send_or_update_player_embed(interaction.guild, interaction.channel)

# ==========================================
# 🚀 봇 실행
# ==========================================
if __name__ == "__main__":
    if not TOKEN:
        print("🚨 [오류] 토큰이 설정되지 않았습니다.")
    else:
        bot.run(TOKEN)