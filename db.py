import os
import sys
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from werkzeug.security import generate_password_hash
from config import Config


def get_db_connection():
    """Connect to PostgreSQL database using configuration."""
    conn = psycopg2.connect(
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        dbname=Config.DB_NAME,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD,
        cursor_factory=psycopg2.extras.RealDictCursor
    )
    return conn


@contextmanager
def get_db():
    """Context manager for safe database operations with auto-commit/rollback.

    Usage:
        with get_db() as (conn, cur):
            cur.execute("INSERT INTO ...")
            # auto-commits on success, auto-rollbacks on exception
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def init_db():
    """Initialize database tables and seed admin account + sample hackathons."""
    conn = get_db_connection()
    cur = conn.cursor()

    # Read schema.sql file
    base_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    schema_path = os.path.join(base_dir, 'database', 'schema.sql')
    if not os.path.exists(schema_path):
        schema_path = os.path.join(os.path.dirname(sys.executable), 'database', 'schema.sql')
    if os.path.exists(schema_path):
        with open(schema_path, 'r', encoding='utf-8') as f:
            sql_schema = f.read()
        cur.execute(sql_schema)
        conn.commit()
        print("Database schema loaded successfully.")

    # Fix: Ensure accounts table exists and legacy registrations table is cleaned up
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id SERIAL PRIMARY KEY,
                user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            DROP TABLE IF EXISTS registrations CASCADE;
        """)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Note: Accounts migration skipped: {e}")

    # Fix: Ensure new columns exist on users and hackathons tables
    try:
        cur.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS age INT DEFAULT 18;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS passport_pfp VARCHAR(255);
            ALTER TABLE users ADD COLUMN IF NOT EXISTS birth_certificate VARCHAR(255);
            ALTER TABLE users ADD COLUMN IF NOT EXISTS aadhaar_card VARCHAR(255);
            ALTER TABLE hackathons ADD COLUMN IF NOT EXISTS min_age INT DEFAULT 18;
            ALTER TABLE hackathons ADD COLUMN IF NOT EXISTS max_age INT DEFAULT 100;
        """)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Note: Column migration skipped: {e}")

    # Fix: Remove duplicate site_visitors rows (keep the latest per session_id)
    try:
        cur.execute("""
            DELETE FROM site_visitors a USING site_visitors b
            WHERE a.id < b.id AND a.session_id = b.session_id;
        """)
        if cur.rowcount > 0:
            print(f"Cleaned up {cur.rowcount} duplicate site_visitor rows.")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Note: site_visitors cleanup skipped: {e}")

    # Fix: Ensure UNIQUE constraint exists on site_visitors.session_id
    # (CREATE TABLE IF NOT EXISTS won't add constraints to an existing table)
    try:
        cur.execute("""
            SELECT 1 FROM pg_constraint
            WHERE conname = 'site_visitors_session_id_key'
              AND conrelid = 'site_visitors'::regclass;
        """)
        if not cur.fetchone():
            cur.execute("""
                ALTER TABLE site_visitors
                ADD CONSTRAINT site_visitors_session_id_key UNIQUE (session_id);
            """)
            conn.commit()
            print("Added missing UNIQUE constraint on site_visitors.session_id.")
    except Exception as e:
        conn.rollback()
        print(f"Note: UNIQUE constraint check skipped: {e}")

    # Fix: Ensure team_members status check allows 'invited'
    try:
        cur.execute("""
            ALTER TABLE team_members DROP CONSTRAINT IF EXISTS team_members_status_check;
            ALTER TABLE team_members ADD CONSTRAINT team_members_status_check CHECK (status IN ('accepted', 'pending', 'rejected', 'invited'));
        """)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Note: team_members status constraint update skipped: {e}")

    # ------------------------------------------------------------------
    # Seed admin user & account
    # ------------------------------------------------------------------
    cur.execute("SELECT id FROM users WHERE email = %s OR role = 'admin';", (Config.ADMIN_EMAIL,))
    admin = cur.fetchone()
    if not admin:
        admin_pass_hash = generate_password_hash(Config.ADMIN_PASSWORD)
        sec_a1_hash = generate_password_hash('blue')
        sec_a2_hash = generate_password_hash('dog')

        cur.execute("""
            INSERT INTO users (
                full_name, email, phone, age, password_hash, institution, skills,
                security_q1, security_a1_hash, security_q2, security_a2_hash, role
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) RETURNING id;
        """, (
            'System Administrator',
            Config.ADMIN_EMAIL,
            '1234567890',
            30,
            admin_pass_hash,
            'Hackathon Hub HQ',
            'Platform Management, Security, Fullstack',
            'What is your favorite color?',
            sec_a1_hash,
            'What is your first pet name?',
            sec_a2_hash,
            'admin'
        ))
        admin_id = cur.fetchone()['id']
        cur.execute("INSERT INTO accounts (user_id) VALUES (%s);", (admin_id,))
        conn.commit()
        print(f"Default admin created ({Config.ADMIN_USERNAME} / {Config.ADMIN_EMAIL}).")
    else:
        # Ensure account row exists for existing admin
        cur.execute("SELECT id FROM accounts WHERE user_id = %s;", (admin['id'],))
        if not cur.fetchone():
            cur.execute("INSERT INTO accounts (user_id) VALUES (%s);", (admin['id'],))
            conn.commit()

    # ------------------------------------------------------------------
    # Seed sample hackathons
    # ------------------------------------------------------------------
    samples = [
        (
            'Global AI & Web3 Hackathon 2026',
            'AI / ML',
            '🤖',
            'The world\'s largest AI & Web3 hackathon. Join 1,000+ builders worldwide to create innovative solutions.',
            'Empowering developers to combine Artificial Intelligence and Decentralized Technologies. Build LLM agents, smart contracts, zero-knowledge proofs, and automated workflows. Mentors from top tech companies will guide teams throughout the event.',
            "NOW() - INTERVAL '1 day'",
            "NOW() + INTERVAL '3 days'"
        ),
        (
            'FinTech Forge 2026',
            'FinTech',
            '💳',
            'Build groundbreaking financial applications. From DeFi protocols to micro-investing and automated banking.',
            'Design modern solutions for international transfers, open banking APIs, fraud detection systems, and DeFi financial access. Showcase your product to venture capital firms and financial technology leaders.',
            "NOW() - INTERVAL '2 days'",
            "NOW() + INTERVAL '5 days'"
        ),
        (
            'ClimateTech Hackathon 2026',
            'ClimateTech',
            '🌿',
            'Build climate-positive tech solutions. From carbon tracking to smart energy grids and sustainability apps.',
            'Addressing the urgent challenges of global climate change with software innovation. Create IoT energy monitors, supply chain tracking, carbon footprint calculators, or sustainable agriculture platforms.',
            "NOW() + INTERVAL '5 days'",
            "NOW() + INTERVAL '10 days'"
        ),
        (
            'HealthTech Innovators 2026',
            'HealthTech',
            '💊',
            'Transform patient care, diagnostic AI, telemedicine, and health record accessibility.',
            'Bridging computer science and medical technology. Build diagnostic imaging assistants, wearable data aggregators, or mental health support platforms.',
            "NOW() - INTERVAL '10 days'",
            "NOW() - INTERVAL '2 days'"
        ),
        (
            'Game Dev Galaxy 2026',
            'Game Dev',
            '🎮',
            'Build immersive 2D/3D games, WebGL experiences, or interactive physics-based web games.',
            'A 48-hour game jam focused on creativity, gameplay mechanics, storytelling, and graphics optimization. Submissions will be judged on gameplay, art, and sound design.',
            "NOW() + INTERVAL '12 days'",
            "NOW() + INTERVAL '15 days'"
        )
    ]

    for s_name, s_cat, s_logo, s_short, s_full, s_start, s_end in samples:
        cur.execute("SELECT id FROM hackathons WHERE name = %s;", (s_name,))
        if not cur.fetchone():
            cur.execute(f"""
                INSERT INTO hackathons (name, category, logo_url, short_description, full_description, start_time, end_time)
                VALUES (%s, %s, %s, %s, %s, {s_start}, {s_end});
            """, (s_name, s_cat, s_logo, s_short, s_full))
            conn.commit()

    cur.close()
    conn.close()


if __name__ == '__main__':
    init_db()
