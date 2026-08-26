import os
import aiosqlite
import config

DB_PATH = config.DB_PATH

async def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                guild_id INTEGER,
                age_group TEXT,
                dating_pool TEXT DEFAULT 'ADULT',
                gender TEXT,
                interested_in TEXT,
                location TEXT,
                dating_eligible BOOLEAN DEFAULT 1,
                dating_enabled BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                guild_id INTEGER,
                bio TEXT,
                photos TEXT,
                primary_photo TEXT,
                dating_intent TEXT,
                interests TEXT,
                show_rating BOOLEAN DEFAULT 1,
                dating_enabled BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS preferences (
                user_id INTEGER PRIMARY KEY,
                min_age INTEGER DEFAULT 18,
                max_age INTEGER DEFAULT 99,
                preferred_genders TEXT,
                preferred_regions TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS likes (
                liker_id INTEGER,
                target_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (liker_id, target_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS passes (
                user_id INTEGER,
                target_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, target_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                user_id INTEGER,
                blocked_user_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, blocked_user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                match_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_a INTEGER,
                user_b INTEGER,
                status TEXT DEFAULT 'ACTIVE',
                ticket_channel_id INTEGER,
                voice_channel_id INTEGER,
                voice_empty_since TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                closed_at TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rating_sessions (
                session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_user_id INTEGER,
                gender TEXT,
                status TEXT DEFAULT 'ACTIVE',
                submitted_photos TEXT,
                minimum_votes INTEGER DEFAULT 3,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                completed_at TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                rating_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                rater_id INTEGER,
                target_id INTEGER,
                overall_score REAL,
                face_score REAL,
                physique_score REAL,
                style_score REAL,
                valid BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(session_id, rater_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rating_results (
                user_id INTEGER PRIMARY KEY,
                rating_count INTEGER DEFAULT 0,
                overall_average REAL,
                face_average REAL,
                physique_average REAL,
                style_average REAL,
                tier TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS xp (
                user_id INTEGER PRIMARY KEY,
                total_xp INTEGER DEFAULT 0,
                level INTEGER DEFAULT 1,
                last_msg_at TIMESTAMP,
                daily_xp INTEGER DEFAULT 0,
                last_daily_reset TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS confessions (
                confession_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                user_id INTEGER,
                content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Media tickets table for attachment-based profile photo/video uploads.
        # mode: 'replace' (new profile or Clear & Replace) or 'append' (Add Media
        # to an existing, incomplete media set). max_items: how many uploads this
        # specific ticket will accept (e.g. remaining slots for an append ticket).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS photo_tickets (
                ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmed BOOLEAN DEFAULT 0,
                mode TEXT DEFAULT 'replace',
                max_items INTEGER DEFAULT 5
            )
        """)

        # Performance Indexes
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_dating ON users(dating_enabled, dating_eligible, dating_pool, gender)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_likes_liker_target ON likes(liker_id, target_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_blocks_user_target ON blocks(user_id, blocked_user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_matches_users_status ON matches(user_a, user_b, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_xp_user ON xp(total_xp)")

        await db.commit()
