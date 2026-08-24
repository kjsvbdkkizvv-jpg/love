import os
import discord
from dotenv import load_dotenv

load_dotenv()

# Bot Authentication Token & Database Path
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DB_PATH", "/app/data/looksmatch.db")

# Theme Color Configuration (#794162)
PRIMARY_COLOR = discord.Color.from_str("#794162")

# Channel & Category Explicit ID Configuration
CHANNEL_DISCOVER = int(os.getenv("CHANNEL_DISCOVER", "1541041359769706586"))
CHANNEL_START_DATING = CHANNEL_DISCOVER  # Alias for discovery
CHANNEL_MY_PROFILE = int(os.getenv("CHANNEL_MY_PROFILE", "1541041398269091881"))
CATEGORY_MATCHES = int(os.getenv("CATEGORY_MATCHES", "1541038109024714752"))
CATEGORY_MATCH_VOICE = int(os.getenv("CATEGORY_MATCH_VOICE", "1541038177274564608"))
CHANNEL_LIKED_YOU = int(os.getenv("CHANNEL_LIKED_YOU", "1541052599455719605"))
CHANNEL_GET_RATED = int(os.getenv("CHANNEL_GET_RATED", "1541043188117282936"))
CHANNEL_MY_RATING = int(os.getenv("CHANNEL_MY_RATING", "1541043225257705552"))
CHANNEL_PROFILE_CHECK = int(os.getenv("CHANNEL_PROFILE_CHECK", "1541043286280642610"))
CHANNEL_CONFESSIONS = int(os.getenv("CHANNEL_CONFESSIONS", "1541044196956438538"))
CHANNEL_CONFESSION_LOGS = int(os.getenv("CHANNEL_CONFESSION_LOGS", "1541065281613336636"))
CHANNEL_SERVER_STATS = int(os.getenv("CHANNEL_SERVER_STATS", "1541065478502613043"))

# Photo ticket category (where private upload tickets are created). Falls back to CATEGORY_MATCHES if unset.
CATEGORY_PHOTO_TICKETS = int(os.getenv("CATEGORY_PHOTO_TICKETS", "1541038079920443462"))

# Roles
ROLE_OWNER = int(os.getenv("ROLE_OWNER", "1540666072154382406"))
ROLE_STAFF = int(os.getenv("ROLE_STAFF", "1540669301495701565"))
ROLE_VERIFIED = int(os.getenv("ROLE_VERIFIED", "1540669305157451818"))
ROLE_VERIFIED_RATER = int(os.getenv("ROLE_VERIFIED_RATER", "1540669303660220507"))
ROLE_LEAD_RATER = int(os.getenv("ROLE_LEAD_RATER", "1540669303085465670"))
ROLE_CREATE_DATING_PROFILE = int(os.getenv("ROLE_CREATE_DATING_PROFILE", "1541029088133648434"))

# Staff Tier Roles Mapping
STAFF_ROLES = {
    "Owner": int(os.getenv("ROLE_OWNER", "1511000000000000099")),
    "Co Owner": int(os.getenv("ROLE_CO_OWNER", "1540669275407388692")),
    "Executive Administrator": int(os.getenv("ROLE_EXEC_ADMIN", "1540669281916944465")),
    "Head Administrator": int(os.getenv("ROLE_HEAD_ADMIN", "1540669285301493840")),
    "Administrator": int(os.getenv("ROLE_ADMINISTRATOR", "1540669288757858324")),
    "Head Moderator": int(os.getenv("ROLE_HEAD_MOD", "1540669291991400488")),
    "Senior Moderator": int(os.getenv("ROLE_SR_MOD", "1540669298287059046")),
    "Moderator": int(os.getenv("ROLE_MODERATOR", "1540669299067461672")),
    "Junior Moderator": int(os.getenv("ROLE_JR_MOD", "1540669299780354059")),
    "Trial Moderator": int(os.getenv("ROLE_TRIAL_MOD", "1540669300745183282")),
}

# Age Role Configuration IDs
AGE_ROLES = {
    "13-15": int(os.getenv("ROLE_AGE_13_15", "1540680836607909908")),
    "16-17": int(os.getenv("ROLE_AGE_16_17", "1540677241598378110")),
    "18-20": int(os.getenv("ROLE_AGE_18_20", "1540677245960585216")),
    "21-24": int(os.getenv("ROLE_AGE_21_25", "1540677249400045639")),
    "25-29": int(os.getenv("ROLE_AGE_26_PLUS", "1540677252986048633")),
    "30-34": int(os.getenv("ROLE_AGE_26_PLUS", "1540677256849006826")),
    "35+": int(os.getenv("ROLE_AGE_26_PLUS", "1540677261047627786")),
}
UNDERAGE_GROUPS = ["13-15", "16-17"]

# Gender Roles
GENDER_ROLES = {
    "Woman": int(os.getenv("ROLE_GENDER_WOMAN", "1540676838878085120")),
    "Man": int(os.getenv("ROLE_GENDER_MAN", "1540676810747158588")),
}

# Preference Roles
INTERESTED_IN_ROLES = {
    "Men": int(os.getenv("ROLE_PREF_MEN", "1540680682496331907")),
    "Women": int(os.getenv("ROLE_PREF_WOMEN", "1540680687290552330")),
    "Everyone": int(os.getenv("ROLE_PREF_EVERYONE", "1540680690868424796")),
}

# Regional Roles
REGION_ROLES = {
    "North America": int(os.getenv("ROLE_REGION_NA", "1540681030954918009")),
    "Europe": int(os.getenv("ROLE_REGION_EU", "1540681018019684412")),
    "Asia": int(os.getenv("ROLE_REGION_ASIA", "1540681014769356810")),
    "Oceania": int(os.getenv("ROLE_REGION_OCEANIA", "1540680983236452443")),
    "South America": int(os.getenv("ROLE_REGION_SA", "1540681022939594862")),
    "Africa": int(os.getenv("ROLE_REGION_Africa", "1540681010222596117")),
}

# Level Milestones
LEVEL_ROLES = {
    1: int(os.getenv("ROLE_LVL_1", "1541018004077412412")),
    5: int(os.getenv("ROLE_LVL_5", "1540669307988475956")),
    10: int(os.getenv("ROLE_LVL_10", "1540669307057602652")),
    15: int(os.getenv("ROLE_LVL_10", "1540669306482728991")),
    20: int(os.getenv("ROLE_LVL_20", "1540669308730875924")),
    25: int(os.getenv("ROLE_LVL_50", "1540676221132603453")),
    30: int(os.getenv("ROLE_LVL_100", "1541018099397173288")),
    40: int(os.getenv("ROLE_LVL_100", "1541018115435921418")),
    50: int(os.getenv("ROLE_LVL_100", "1541018097048096788")),
    60: int(os.getenv("ROLE_LVL_100", "1541018107290718228")),
    75: int(os.getenv("ROLE_LVL_100", "1541018112974000178")),
    100: int(os.getenv("ROLE_LVL_100", "1541018102576185344")),
}

# Rating Tiers & Thresholds
BASE_FEMALE_ROLE_ID = int(os.getenv("BASE_FEMALE_ROLE_ID", "1540676838878085120"))
BASE_MALE_ROLE_ID = int(os.getenv("BASE_MALE_ROLE_ID", "1540676810747158588"))

FEMALE_TIER_ORDER = [
    "Sub 3", "LLTB", "MLTB", "HLTB", "LMTB", "MMTB",
    "HMTB", "LHTB", "MHTB", "HHTB", "Stacylite", "Stacy"
]
FEMALE_TIER_ROLES = {
    "Sub 3": int(os.getenv("ROLE_FEMALE_SUB_3", "1540677892386590790")),
    "LLTB": int(os.getenv("ROLE_LLTB", "1540677888700055552")),
    "MLTB": int(os.getenv("ROLE_MLTB", "1540677885256540211")),
    "HLTB": int(os.getenv("ROLE_HLTB", "1540677881447850064")),
    "LMTB": int(os.getenv("ROLE_LMTB", "1540677878285598822")),
    "MMTB": int(os.getenv("ROLE_MMTB", "1540677874598678630")),
    "HMTB": int(os.getenv("ROLE_HMTB", "1540677870949761045")),
    "LHTB": int(os.getenv("ROLE_LHTB", "1540677867153661962")),
    "MHTB": int(os.getenv("ROLE_MHTB", "1540677862854492190")),
    "HHTB": int(os.getenv("ROLE_HHTB", "1540677859213967461")),
    "Stacylite": int(os.getenv("ROLE_STACYLITE", "1540677855795478638")),
    "Stacy": int(os.getenv("ROLE_STACY", "1540677853538943017")),
}

MALE_TIER_ORDER = [
    "Sub 3", "LLTN", "MLTN", "HLTN", "LMTN", "MMTN",
    "HMTN", "LHTN", "MHTN", "HHTN", "Chadlite", "Chad"
]
MALE_TIER_ROLES = {
    "Sub 3": int(os.getenv("ROLE_MALE_SUB_3", "1540677892386590790")),
    "LLTN": int(os.getenv("ROLE_LLTN", "1540680280124162068")),
    "MLTN": int(os.getenv("ROLE_MLTN", "1540680277037420604")),
    "HLTN": int(os.getenv("ROLE_HLTN", "1540680273392308234")),
    "LMTN": int(os.getenv("ROLE_LMTN", "1540680270192185464")),
    "MMTN": int(os.getenv("ROLE_MMTN", "1540680267633786951")),
    "HMTN": int(os.getenv("ROLE_HMTN", "1540680263389155400")),
    "LHTN": int(os.getenv("ROLE_LHTN", "1540680260092297386")),
    "MHTN": int(os.getenv("ROLE_MHTN", "1540680256912891934")),
    "HHTN": int(os.getenv("ROLE_HHTN", "1540680253767421952")),
    "Chadlite": int(os.getenv("ROLE_CHADLITE", "1540680250210390046")),
    "Chad": int(os.getenv("ROLE_CHAD", "1540680246611939368")),
}

SCORE_SCALE_MAX = 10.0
MINIMUM_VERIFIED_VOTES = 3
WEIGHT_ALREADY_LIKED = 100

# UI Component Identifiers
ID_START_DATING = "ui:start_dating"
ID_VIEW_PROFILE = "ui:view_profile"
ID_EDIT_PROFILE = "ui:edit_profile"
ID_EDIT_PHOTOS = "ui:edit_photos"
ID_PAUSE_DATING = "ui:pause_dating"
ID_PREFERENCES = "ui:preferences"
ID_VIEW_LIKED_YOU = "ui:view_liked_you"
ID_MY_RATING_VIEW = "ui:my_rating_view"
ID_RATING_START = "ui:rating_start"
ID_RATING_RESULTS = "ui:rating_results"
ID_MATCHES_VIEW = "ui:matches_view"
ID_DISCOVERY_LIKE = "discovery:like"
ID_DISCOVERY_PASS = "discovery:pass"
ID_DISCOVERY_BLOCK = "discovery:block"
ID_DISCOVERY_REPORT = "discovery:report"
ID_MATCH_VOICE = "match:voice"
ID_MATCH_REPORT = "match:report"
ID_MATCH_CLOSE = "match:close"
ID_ONBOARDING_SETUP_PROFILE = "onboarding:setup_profile"
