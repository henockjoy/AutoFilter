import logging
from struct import pack
import re
import base64
from pyrogram.file_id import FileId
from pymongo.errors import DuplicateKeyError
from umongo import Instance, Document, fields
from motor.motor_asyncio import AsyncIOMotorClient
from marshmallow.exceptions import ValidationError
from info import CAPTION_LANGUAGES, DATABASE_URI, DATABASE_NAME, COLLECTION_NAME, USE_CAPTION_FILTER, MAX_B_TN, MOVIE_UPDATE_CHANNEL, OWNERID
from utils import get_settings, save_group_settings, temp, get_status
from database.users_chats_db import add_name
from .Imdbposter import get_movie_details, fetch_image
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
#---------------------------------------------------------
# Some basic variables needed
tempDict = {'indexDB': DATABASE_URI}

# Primary DB
client = AsyncIOMotorClient(DATABASE_URI)
db = client[DATABASE_NAME]
instance = Instance.from_db(db)


# Primary DB Model
@instance.register
class Media(Document):
    file_id = fields.StrField(attribute='_id')
    file_ref = fields.StrField(allow_none=True)
    file_name = fields.StrField(required=True)
    file_size = fields.IntField(required=True)
    file_type = fields.StrField(allow_none=True)
    mime_type = fields.StrField(allow_none=True)
    caption = fields.StrField(allow_none=True)

    class Meta:
        indexes = ('$file_name', )
        collection_name = COLLECTION_NAME

async def choose_mediaDB():
    global saveMedia
    saveMedia = Media
    logger.info("Using first db (Media)")

async def save_file(bot, media):
  """Save file in database"""
  global saveMedia
  file_id, file_ref = unpack_new_file_id(media.file_id)
  file_name = re.sub(r"(_|\-|\.|\+)", " ", str(media.file_name))
  try:
    file = saveMedia(
        file_id=file_id,
        file_ref=file_ref,
        file_name=file_name,
        file_size=media.file_size,
        file_type=media.file_type,
        mime_type=media.mime_type,
        caption=media.caption.html if media.caption else None,
    )
  except ValidationError:
    logger.exception('Error occurred while saving file in database')
    return False, 2
  else:
    try:
      await file.commit()
    except DuplicateKeyError:
      logger.warning(f'{getattr(media, "file_name", "NO_FILE")} is already saved in database')   
      return False, 0
    else:
        logger.info(f'{getattr(media, "file_name", "NO_FILE")} is saved to database')
        if await get_status(bot.me.id):
            await send_msg(bot, file.file_name, file.caption)
        return True, 1

async def get_search_results(chat_id, query, file_type=None, max_results=10, offset=0, filter=False):
    """For given query return (results, next_offset)"""
    if chat_id is not None:
        settings = await get_settings(int(chat_id))
        try:
            if settings['max_btn']:
                max_results = 10
            else:
                max_results = int(MAX_B_TN)
        except KeyError:
            await save_group_settings(int(chat_id), 'max_btn', False)
            settings = await get_settings(int(chat_id))
            if settings['max_btn']:
                max_results = 10
            else:
                max_results = int(MAX_B_TN)
    query = query.strip()
    if not query:
        raw_pattern = '.'
    elif ' ' not in query:
        raw_pattern = r'(\b|[\.\+\-_])' + query + r'(\b|[\.\+\-_])'
    else:
        raw_pattern = query.replace(' ', r'.*[\s\.\+\-_()]')
    
    try:
        regex = re.compile(raw_pattern, flags=re.IGNORECASE)
    except:
        return []

    if USE_CAPTION_FILTER:
        filter = {'$or': [{'file_name': regex}, {'caption': regex}]}
    else:
        filter = {'file_name': regex}

    if file_type:
        filter['file_type'] = file_type

    total_results = await Media.count_documents(filter)

    #verifies max_results is an even number or not
    if max_results%2 != 0: 
        logger.info(f"Since max_results is an odd number ({max_results}), bot will use {max_results+1} as max_results to make it even.")
        max_results += 1

    cursor = Media.find(filter)
    cursor.sort('$natural', -1)
    cursor.skip(offset).limit(max_results)

    files = await cursor.to_list(length=max_results)
    next_offset = offset + len(files)
    if next_offset >= total_results:
        next_offset = ''
    return files, next_offset, total_results

async def get_bad_files(query, file_type=None, filter=False):
    """For given query return (results, next_offset)"""
    query = query.strip()
    if not query:
        raw_pattern = '.'
    elif ' ' not in query:
        raw_pattern = r'(\b|[\.\+\-_])' + query + r'(\b|[\.\+\-_])'
    else:
        raw_pattern = query.replace(' ', r'.*[\s\.\+\-_()]')
    
    try:
        regex = re.compile(raw_pattern, flags=re.IGNORECASE)
    except:
        return []

    if USE_CAPTION_FILTER:
        filter = {'$or': [{'file_name': regex}, {'caption': regex}]}
    else:
        filter = {'file_name': regex}

    if file_type:
        filter['file_type'] = file_type

    cursor = Media.find(filter)
    cursor.sort('$natural', -1)

    files = await cursor.to_list(length=(await Media.count_documents(filter)))
    total_results = len(files)

    return files, total_results

async def get_file_details(query):
    filter = {'file_id': query}
    cursor = Media.find(filter)
    filedetails = await cursor.to_list(length=1)
    return filedetails


def encode_file_id(s: bytes) -> str:
    r = b""
    n = 0

    for i in s + bytes([22]) + bytes([4]):
        if i == 0:
            n += 1
        else:
            if n:
                r += b"\x00" + bytes([n])
                n = 0

            r += bytes([i])

    return base64.urlsafe_b64encode(r).decode().rstrip("=")

def encode_file_ref(file_ref: bytes) -> str:
    return base64.urlsafe_b64encode(file_ref).decode().rstrip("=")

def unpack_new_file_id(new_file_id):
    """Return file_id, file_ref"""
    decoded = FileId.decode(new_file_id)
    file_id = encode_file_id(
        pack(
            "<iiqq",
            int(decoded.file_type),
            decoded.dc_id,
            decoded.media_id,
            decoded.access_hash
        )
    )
    file_ref = encode_file_ref(decoded.file_reference)
    return file_id, file_ref
# ------------------------------
# Duplicate Check
# ------------------------------
announced_collection = db["Announced"]

async def already_announced(title: str) -> bool:
    exists = await announced_collection.find_one({"title": title})
    return exists is not None

async def mark_announced(title: str):
    await announced_collection.insert_one({"title": title})


# ------------------------------
# Announce New File
# ------------------------------
async def send_msg(bot, filename, caption=""):
    try:
        # --- Clean filename ---
        filename = re.sub(r'\(\@\S+\)|\[\@\S+\]|\b@\S+|\bwww\.\S+', '', filename).strip()

        # Detect year
        year_match = re.search(r"\b(19|20)\d{2}\b", filename)
        year = year_match.group(0) if year_match else None

        # Detect Season/Episode
        season_match = re.search(r"(S\d{1,2})", filename, re.I)
        episode_match = re.search(r"(E\d{1,2})", filename, re.I)

        if season_match:  # TV Series
            file_type = "𝖳𝖵𝖲𝖤𝖱𝖨𝖤𝖲"
            if episode_match:
                clean_title = f"{filename.split(season_match.group(1))[0].strip()} {season_match.group(1).upper()}{episode_match.group(1).upper()}"
            else:
                clean_title = f"{filename.split(season_match.group(1))[0].strip()} {season_match.group(1).upper()}"
        else:  # Movie
            file_type = "𝖬𝖮𝖵𝖨𝖤"
            if year:
                clean_title = f"{filename.split(year)[0].strip()} {year}"
            else:
                clean_title = filename.split(".")[0].strip()

        clean_title = re.sub(r"[\(\)\[\]\{\}:;'\-!.,_]+", " ", clean_title).strip()
        normalized_title = clean_title.lower().strip()

        # --- Duplicate check ---
        if await already_announced(normalized_title):
            logger.info(f"Skipping duplicate announcement for {clean_title}")
            return

        # --- Get IMDb + Trailer details (already implemented elsewhere) ---
        details = await get_movie_details(clean_title)  # returns dict with imdb_url, rating, genres, trailer_url
        imdb_url = details.get("imdb_url") if details else None
        imdb_rating = details.get("rating") if details else None
        genres = details.get("genres") if details else []
        trailer_url = details.get("trailer_url") if details else None

        # --- Build caption ---
        text = f"✅ {clean_title} #{file_type}\n\n"
        if caption:
            text += caption + "\n"

        # Ratings
        rating_links = []
        if imdb_rating:
            rating_links.append(f"⭐ {imdb_rating}/10")
        if imdb_url:
            rating_links.append(f"<a href='{imdb_url}'>⭐ IMDb</a>")
        if rating_links:
            text += " | ".join(rating_links) + "\n"

        # Genres
        if genres:
            text += "📽 " + ", ".join(genres[:2]) + "\n"  # max 2 genres

        # --- Buttons ---
        btn = []
        if trailer_url:
            btn.append([InlineKeyboardButton("▶️ 𝖶𝖺𝗍𝖼𝗁 𝖳𝗋𝖺𝗂𝗅𝖾𝗋", url=trailer_url)])
        btn.append([InlineKeyboardButton(
            '📁 𝖢𝗅𝗂𝖼𝗄 𝗍𝗈 𝗌𝖾𝖺𝗋𝖼𝗁',
            url=f"https://telegram.me/{temp.U_NAME}?start=getfile-{clean_title.replace(' ', '-')}"
        )])

        # --- Send message ---
        await bot.send_message(
            chat_id=MOVIE_UPDATE_CHANNEL,
            text=text,
            reply_markup=InlineKeyboardMarkup(btn)
        )

        # --- Mark as announced ---
        await mark_announced(normalized_title)

    except Exception as e:
        logger.error(f"Error in send_msg: {e}", exc_info=True)


async def get_qualities(text, qualities: list):
    """Get all Quality from text"""
    quality = []
    for q in qualities:
        if q in text:
            quality.append(q)
    quality = ", ".join(quality)
    return quality[:-2] if quality.endswith(", ") else quality






