import os
import discord
from dotenv import load_dotenv

load_dotenv()

# Bot Authentication Token & Database Path
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DB_PATH", "looksmatch.db")

# Theme Color Configuration (#794162)
PRIMARY_COLOR = discord.Color.from_str("#794162")

# Server Role Configuration IDs
ROLE_OWNER = int(os.getenv("ROLE_OWNER", "1511000000000000099"))
ROLE_STAFF = int(os.getenv("ROLE_STAFF", "1540669301495701565"))

# Onboarding Profile Creation Role ID
ROLE_CREATE_DATING_PROFILE = int(os.getenv("ROLE_CREATE_DATING_PROFILE", "1541029088133648434"))

# Verified Rater Roles
ROLE_VERIFIED_RATER = int(os.getenv("ROLE_VERIFIED_RATER", "1511000000000000080"))
ROLE_LEAD_RATER = int(os.getenv("ROLE_LEAD_RATER", "1511000000000000081"))

# Staff Tier Roles Mapping
STAFF_ROLES = {
    "Owner": int(os.getenv("ROLE_OWNER", "1511000000000000099")),
    "Co Owner": int(os.getenv("ROLE_CO_OWNER", "1511000000000000098")),
    "Executive Administrator": int(os.getenv("ROLE_EXEC_ADMIN", "1511000000000000097")),
    "Head Administrator": int(os.getenv("ROLE_HEAD_ADMIN", "1511000000000000096")),
    "Administrator": int(os.getenv("ROLE_ADMINISTRATOR", "1511000000000000095")),
    "Head Moderator": int(os.getenv("ROLE_HEAD_MOD", "1511000000000000094")),
    "Senior Moderator": int(os.getenv("ROLE_SR_MOD", "1511000000000000093")),
    "Moderator": int(os.getenv("ROLE_MODERATOR", "1511000000000000092")),
    "Junior Moderator": int(os.getenv("ROLE_JR_MOD", "1511000000000000091")),
    "Trial Moderator": int(os.getenv("ROLE_TRIAL_MOD", "1511000000000000090")),
}

# Age Role Configuration IDs
AGE_ROLES = {
    "13-15": int(os.getenv("ROLE_AGE_13_15", "1511000000000000010")),
    "16-17": int(os.getenv("ROLE_AGE_16_17", "1511000000000000011")),
    "18-20": int(os.getenv("ROLE_AGE_18_20", "1511000000000000012")),
    "21-25": int(os.getenv("ROLE_AGE_21_25", "1511000000000000013")),
    "26+": int(os.getenv("ROLE_AGE_26_PLUS", "1511000000000000014")),
}
UNDERAGE_GROUPS = ["13-15", "16-17"]

# Gender Roles
GENDER_ROLES = {
    "Woman": int(os.getenv("ROLE_GENDER_WOMAN", "1511000000000000020")),
    "Man": int(os.getenv("ROLE_GENDER_MAN", "1511000000000000021")),
    "Non-Binary": int(os.getenv("ROLE_GENDER_NB", "1511000000000000022")),
}

# Preference Roles
INTERESTED_IN_ROLES = {
    "Men": int(os.getenv("ROLE_PREF_MEN", "1511000000000000030")),
    "Women": int(os.getenv("ROLE_PREF_WOMEN", "1511000000000000031")),
    "Everyone": int(os.getenv("ROLE_PREF_EVERYONE", "1511000000000000032")),
}

# Regional Roles
REGION_ROLES = {
    "North America": int(os.getenv("ROLE_REGION_NA", "1511000000000000040")),
    "Europe": int(os.getenv("ROLE_REGION_EU", "1511000000000000041")),
    "Asia / Oceania": int(os.getenv("ROLE_REGION_ASIA", "1511000000000000042")),
    "South America": int(os.getenv("ROLE_REGION_SA", "1511000000000000043")),
    "Other": int(os.getenv("ROLE_REGION_OTHER", "1511000000000000044")),
}

# Level Milestones
LEVEL_ROLES = {
    1: int(os.getenv("ROLE_LVL_1", "1511000000000000050")),
    5: int(os.getenv("ROLE_LVL_5", "1511000000000000051")),
    10: int(os.getenv("ROLE_LVL_10", "1511000000000000052")),
    20: int(os.getenv("ROLE_LVL_20", "1511000000000000053")),
    50: int(os.getenv("ROLE_LVL_50", "1511000000000000054")),
    100: int(os.getenv("ROLE_LVL_100", "1511000000000000055")),
}

# Rating Tiers & Thresholds
BASE_FEMALE_ROLE_ID = int(os.getenv("BASE_FEMALE_ROLE_ID", "1511000000000000060"))
BASE_MALE_ROLE_ID = int(os.getenv("BASE_MALE_ROLE_ID", "1511000000000000061"))

FEMALE_TIER_ORDER = ["Stacy Lite", "Stacy", "High Stacy"]
FEMALE_TIER_ROLES = {
    "Stacy Lite": int(os.getenv("ROLE_STACY_LITE", "1511000000000000062")),
    "Stacy": int(os.getenv("ROLE_STACY", "1511000000000000063")),
    "High Stacy": int(os.getenv("ROLE_HIGH_STACY", "1511000000000000064")),
}

MALE_TIER_ORDER = ["Chad Lite", "Chad", "High Chad"]
MALE_TIER_ROLES = {
    "Chad Lite": int(os.getenv("ROLE_CHAD_LITE", "1511000000000000072")),
    "Chad": int(os.getenv("ROLE_CHAD", "1511000000000000073")),
    "High Chad": int(os.getenv("ROLE_HIGH_CHAD", "1511000000000000074")),
}

SCORE_SCALE_MAX = 10.0
MINIMUM_VERIFIED_VOTES = 3
WEIGHT_ALREADY_LIKED = 100

# UI Component Identifiers
ID_START_DATING = "ui:start_dating"
ID_VIEW_PROFILE = "ui:view_profile"
ID_EDIT_PROFILE = "ui:edit_profile"
ID_EDIT_PHOTOS = "ui:edit_photos"
ID_PREFERENCES = "ui:preferences"
ID_PAUSE_DATING = "ui:pause_dating"
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

# Server Structure Definition
SERVER_STRUCTURE = {
    "📌 START HERE": ["welcome", "rules", "announcements", "start-dating"],
    "💬 COMMUNITY": ["general", "my-profile", "rate-me", "rating-results", "my-matches"],
    "💞 MATCHES": [],
    "🔊 VOICE": ["Lounge", "Date Room 1", "Date Room 2"],
    "🛡️ STAFF": ["dating-reports", "server-stats"]
}
