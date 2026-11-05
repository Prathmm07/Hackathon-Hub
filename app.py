import os, sys, uuid
from functools import wraps
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

from config import Config
from db import get_db, init_db

# Configure paths for standard execution and frozen PyInstaller standalone executable
if getattr(sys, 'frozen', False):
    base_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    exe_dir = os.path.dirname(sys.executable)
    app = Flask(__name__,
                template_folder=os.path.join(base_dir, 'templates'),
                static_folder=os.path.join(base_dir, 'static'))
    UPLOAD_FOLDER = os.path.join(exe_dir, 'static', 'uploads')
else:
    app = Flask(__name__)
    UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')

app.config.from_object(Config)

# Ensure persistent uploads are served reliably across both script and exe execution
@app.route('/static/uploads/<path:filename>')
def serve_uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'webp'}

# Initialize database schema and seeds on startup
with app.app_context():
    try:
        init_db()
    except Exception as e:
        print(f"Database startup warning: {e}")


# ==================== HELPERS & UTILITIES ====================

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def save_uploaded_file(file_obj, subfolder):
    """Saves uploaded file with a unique UUID name and returns its static URL."""
    if file_obj and file_obj.filename and allowed_file(file_obj.filename):
        ext = file_obj.filename.rsplit('.', 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        target_dir = os.path.join(UPLOAD_FOLDER, subfolder)
        os.makedirs(target_dir, exist_ok=True)
        file_obj.save(os.path.join(target_dir, filename))
        return f"/static/uploads/{subfolder}/{filename}"
    return None


def fetch_all(sql, params=()):
    """Executes a SQL SELECT query and returns all matching rows as dictionaries."""
    with get_db() as (conn, cur):
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(sql, params=()):
    """Executes a SQL SELECT query and returns the first row or None."""
    with get_db() as (conn, cur):
        cur.execute(sql, params)
        return cur.fetchone()


def execute_sql(sql, params=(), returning_id=False):
    """Executes an INSERT/UPDATE/DELETE query with auto-commit."""
    with get_db() as (conn, cur):
        cur.execute(sql, params)
        if returning_id:
            row = cur.fetchone()
            return row['id'] if row else None
        return cur.rowcount


def get_hackathon_status(start_time, end_time):
    """Dynamically computes hackathon status (Live, Upcoming, Ended) in UTC."""
    now = datetime.now(timezone.utc)
    s = start_time.replace(tzinfo=timezone.utc) if start_time.tzinfo is None else start_time
    e = end_time.replace(tzinfo=timezone.utc) if end_time.tzinfo is None else end_time
    return 'Upcoming' if now < s else ('Live' if now < e else 'Ended')


def get_lockout_duration(failed_count):
    """Calculates progressive account lockout duration in seconds."""
    return {3: 30, 4: 60, 5: 120, 6: 240}.get(failed_count, 480) if failed_count >= 3 else 0


def to_int(val, default=0):
    """Safely converts input value to integer."""
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


# ==================== ACCESS CONTROL DECORATORS ====================

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or session.get('user_role') != 'admin':
            flash('Administrator privileges required.', 'danger')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function


@app.context_processor
def inject_global_data():
    """Provides unread notification count across navigation bars."""
    unread_count = 0
    if 'user_id' in session:
        res = fetch_one("""
            SELECT COUNT(*) as count FROM notifications 
            WHERE (user_id = %s OR user_id IS NULL) AND is_read = FALSE;
        """, (session['user_id'],))
        if res:
            unread_count = res['count']
    return dict(unread_notif_count=unread_count)


@app.before_request
def track_visitor():
    """Tracks visitor activity per session in PostgreSQL."""
    if request.path.startswith('/static'):
        return
    ua = request.headers.get('User-Agent', '').lower()
    if any(bot in ua for bot in ['python', 'urllib', 'curl', 'wget', 'bot', 'spider', 'crawler', 'headless']):
        return
    if not session.get('visitor_session'):
        session['visitor_session'] = str(uuid.uuid4())
    try:
        execute_sql("""
            INSERT INTO site_visitors (ip_address, session_id, user_id, last_activity)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (session_id) DO UPDATE SET
                user_id = COALESCE(EXCLUDED.user_id, site_visitors.user_id),
                last_activity = NOW(),
                ip_address = EXCLUDED.ip_address;
        """, (request.remote_addr or '127.0.0.1', session['visitor_session'], session.get('user_id')))
    except Exception:
        pass


# ==================== PUBLIC & AUTHENTICATION ROUTES ====================

@app.route('/')
def index():
    hackathons = fetch_all("SELECT * FROM hackathons ORDER BY start_time ASC;")
    for h in hackathons:
        h['status'] = get_hackathon_status(h['start_time'], h['end_time'])
    live_hackathons = [h for h in hackathons if h['status'] == 'Live']
    return render_template('index.html', hackathons=hackathons, live_hackathons=live_hackathons)


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        age = to_int(request.form.get('age', 0), 0)
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        institution = request.form.get('institution', '').strip()
        skills = request.form.get('skills', '').strip()
        github = request.form.get('github', '').strip()
        linkedin = request.form.get('linkedin', '').strip()
        security_q1 = request.form.get('security_q1', '').strip()
        security_a1 = request.form.get('security_a1', '').strip().lower()
        security_q2 = request.form.get('security_q2', '').strip()
        security_a2 = request.form.get('security_a2', '').strip().lower()

        if age < 18:
            flash('Registration failed: You must be at least 18 years old (18+) to register.', 'danger')
            return render_template('auth/signup.html', form_data=request.form)

        if password != confirm_password:
            flash('Passwords do not match.', 'danger')
            return render_template('auth/signup.html', form_data=request.form)

        if fetch_one("SELECT id FROM users WHERE email = %s;", (email,)):
            flash('An account with this email address already exists.', 'danger')
            return render_template('auth/signup.html', form_data=request.form)

        pfp_file = request.files.get('passport_pfp')
        birth_file = request.files.get('birth_certificate')
        aadhaar_file = request.files.get('aadhaar_card')

        if not (pfp_file and pfp_file.filename):
            flash('Registration failed: Passport PFP profile photo is mandatory.', 'danger')
            return render_template('auth/signup.html', form_data=request.form)
        if not (birth_file and birth_file.filename):
            flash('Registration failed: Birth certificate document is mandatory.', 'danger')
            return render_template('auth/signup.html', form_data=request.form)
        if not (aadhaar_file and aadhaar_file.filename):
            flash('Registration failed: Aadhaar card document is mandatory.', 'danger')
            return render_template('auth/signup.html', form_data=request.form)

        passport_pfp = save_uploaded_file(pfp_file, 'passport_pfps')
        birth_certificate = save_uploaded_file(birth_file, 'documents')
        aadhaar_card = save_uploaded_file(aadhaar_file, 'documents')

        if not (passport_pfp and birth_certificate and aadhaar_card):
            flash('Registration failed: Invalid document file format. Allowed formats: PNG, JPG, JPEG, GIF, PDF, WEBP.', 'danger')
            return render_template('auth/signup.html', form_data=request.form)

        user_id = execute_sql("""
            INSERT INTO users (
                full_name, email, phone, age, passport_pfp, birth_certificate, aadhaar_card,
                password_hash, institution, skills, github, linkedin,
                security_q1, security_a1_hash, security_q2, security_a2_hash
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
        """, (
            full_name, email, phone, age, passport_pfp, birth_certificate, aadhaar_card,
            generate_password_hash(password), institution, skills, github, linkedin,
            security_q1, generate_password_hash(security_a1), security_q2, generate_password_hash(security_a2)
        ), returning_id=True)

        execute_sql("INSERT INTO accounts (user_id) VALUES (%s);", (user_id,))
        execute_sql("INSERT INTO notifications (user_id, title, message) VALUES (%s, %s, %s);",
                    (user_id, 'Welcome to Hackathon Hub!', 'Your account has been created successfully.'))

        flash('Registration successful! Please log in to your account.', 'success')
        return redirect(url_for('login'))

    return render_template('auth/signup.html', form_data={})


@app.route('/login', methods=['GET', 'POST'])
def login():
    lock_message = None
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        user = fetch_one("SELECT * FROM users WHERE email = %s;", (email,))
        if not user:
            flash('Invalid email address or password.', 'danger')
            return render_template('auth/login.html')

        # Progressive lockout check
        now = datetime.now(timezone.utc)
        lock_until = user['lock_until']
        if lock_until:
            if lock_until.tzinfo is None:
                lock_until = lock_until.replace(tzinfo=timezone.utc)
            if now < lock_until:
                remaining = int((lock_until - now).total_seconds())
                lock_message = f"Account locked due to repeated failed login attempts. Wait {remaining} seconds."
                return render_template('auth/login.html', lock_message=lock_message)

        if check_password_hash(user['password_hash'], password):
            execute_sql("UPDATE users SET failed_login_count = 0, lock_until = NULL WHERE id = %s;", (user['id'],))
            session.permanent = True
            session['user_id'] = user['id']
            session['user_name'] = user['full_name']
            session['user_email'] = user['email']
            session['user_role'] = user['role']
            if session.get('visitor_session'):
                try:
                    execute_sql("UPDATE site_visitors SET user_id = %s, last_activity = NOW() WHERE session_id = %s;",
                                (user['id'], session['visitor_session']))
                except Exception:
                    pass
            flash(f"Welcome back, {user['full_name']}!", 'success')
            return redirect(url_for('admin_dashboard' if user['role'] == 'admin' else 'browse_hackathons'))

        # Failed attempt handling
        new_failed_count = user['failed_login_count'] + 1
        lock_duration = get_lockout_duration(new_failed_count)

        if lock_duration > 0:
            execute_sql("UPDATE users SET failed_login_count = %s, lock_until = %s WHERE id = %s;",
                        (new_failed_count, now + timedelta(seconds=lock_duration), user['id']))
            lock_message = f"Incorrect password. Account locked for {lock_duration} seconds."
        else:
            execute_sql("UPDATE users SET failed_login_count = %s WHERE id = %s;", (new_failed_count, user['id']))
            flash('Invalid email address or password.', 'danger')

        return render_template('auth/login.html', lock_message=lock_message)

    return render_template('auth/login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out successfully.', 'info')
    return redirect(url_for('login'))


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        step = to_int(request.form.get('step', 1), 1)

        if step == 1:
            email = request.form.get('email', '').strip().lower()
            user = fetch_one("SELECT id, full_name, email, security_q1, security_q2 FROM users WHERE email = %s;", (email,))
            if not user:
                flash('No account found with that email address.', 'danger')
                return render_template('auth/forgot_password.html', step=1)
            return render_template('auth/forgot_password.html', step=2, user=user)

        elif step == 2:
            user_id = request.form.get('user_id')
            a1 = request.form.get('security_a1', '').strip().lower()
            a2 = request.form.get('security_a2', '').strip().lower()
            user = fetch_one("SELECT * FROM users WHERE id = %s;", (user_id,))
            if user and check_password_hash(user['security_a1_hash'], a1) and check_password_hash(user['security_a2_hash'], a2):
                return render_template('auth/forgot_password.html', step=3, user=user)
            flash('Incorrect security answers.', 'danger')
            return render_template('auth/forgot_password.html', step=2, user=user)

        elif step == 3:
            user_id = request.form.get('user_id')
            new_pass = request.form.get('new_password', '')
            confirm_pass = request.form.get('confirm_password', '')
            if new_pass != confirm_pass:
                flash('Passwords do not match.', 'danger')
                return render_template('auth/forgot_password.html', step=3, user={'id': user_id})

            execute_sql("UPDATE users SET password_hash = %s, failed_login_count = 0, lock_until = NULL WHERE id = %s;",
                        (generate_password_hash(new_pass), user_id))
            flash('Password reset successful! You can now log in.', 'success')
            return redirect(url_for('login'))

    return render_template('auth/forgot_password.html', step=1)


# ==================== PROFILE MANAGEMENT ====================

@app.route('/profile')
@login_required
def view_profile():
    uid = session['user_id']
    user = fetch_one("SELECT * FROM users WHERE id = %s;", (uid,))
    account = fetch_one("SELECT * FROM accounts WHERE user_id = %s;", (uid,))
    my_registrations = fetch_all("""
        SELECT DISTINCT h.id as hackathon_id, h.name as hackathon_name, t.id as team_id, t.team_name,
               CASE WHEN t.leader_id = %s THEN 'team_leader' ELSE 'team_member' END as participation_type,
               COALESCE(tm.requested_at, t.created_at) as registered_at
        FROM teams t
        JOIN hackathons h ON t.hackathon_id = h.id
        LEFT JOIN team_members tm ON t.id = tm.team_id AND tm.user_id = %s
        WHERE t.leader_id = %s OR (tm.user_id = %s AND tm.status = 'accepted')
        ORDER BY registered_at DESC;
    """, (uid, uid, uid, uid))

    my_invitations = fetch_all("""
        SELECT tm.id as membership_id, t.id as team_id, t.team_name, t.description as team_desc,
               h.id as hackathon_id, h.name as hackathon_name,
               u.full_name as leader_name, u.passport_pfp as leader_pfp
        FROM team_members tm
        JOIN teams t ON tm.team_id = t.id
        JOIN hackathons h ON t.hackathon_id = h.id
        JOIN users u ON t.leader_id = u.id
        WHERE tm.user_id = %s AND tm.status = 'invited'
        ORDER BY tm.requested_at DESC;
    """, (uid,))

    return render_template('profile.html', user=user, account=account, my_registrations=my_registrations, my_invitations=my_invitations)


@app.route('/profile/update', methods=['POST'])
@login_required
def update_profile():
    uid = session['user_id']
    full_name = request.form.get('full_name', '').strip()
    phone = request.form.get('phone', '').strip()

    if session.get('user_role') == 'admin':
        if not full_name:
            flash('Full name is required.', 'danger')
            return redirect(url_for('view_profile'))

        new_password = request.form.get('new_password', '').strip()
        if new_password:
            if new_password != request.form.get('confirm_password', '').strip():
                flash('Passwords do not match.', 'danger')
                return redirect(url_for('view_profile'))
            execute_sql("UPDATE users SET full_name = %s, phone = %s, password_hash = %s WHERE id = %s;",
                        (full_name, phone, generate_password_hash(new_password), uid))
        else:
            execute_sql("UPDATE users SET full_name = %s, phone = %s WHERE id = %s;", (full_name, phone, uid))

        session['user_name'] = full_name
        flash('Admin profile updated successfully!', 'success')
        return redirect(url_for('view_profile'))

    age = to_int(request.form.get('age', 18), 18)
    if age < 18:
        flash('Profile update failed: Age must be 18 or older.', 'danger')
        return redirect(url_for('view_profile'))

    user = fetch_one("SELECT * FROM users WHERE id = %s;", (uid,))
    new_pfp = save_uploaded_file(request.files.get('passport_pfp'), 'passport_pfps')
    new_birth = save_uploaded_file(request.files.get('birth_certificate'), 'documents')
    new_aadhaar = save_uploaded_file(request.files.get('aadhaar_card'), 'documents')

    execute_sql("""
        UPDATE users 
        SET full_name = %s, phone = %s, age = %s, institution = %s, skills = %s, github = %s, linkedin = %s,
            passport_pfp = COALESCE(%s, passport_pfp),
            birth_certificate = COALESCE(%s, birth_certificate),
            aadhaar_card = COALESCE(%s, aadhaar_card)
        WHERE id = %s;
    """, (
        full_name, phone, age, request.form.get('institution', '').strip(),
        request.form.get('skills', '').strip(), request.form.get('github', '').strip(),
        request.form.get('linkedin', '').strip(), new_pfp, new_birth, new_aadhaar, uid
    ))

    session['user_name'] = full_name
    flash('Profile updated successfully!', 'success')
    return redirect(url_for('view_profile'))


# ==================== HACKATHON BROWSING & DETAILS ====================

@app.route('/hackathons')
def browse_hackathons():
    selected_statuses = request.args.getlist('status')
    selected_categories = request.args.getlist('category')

    categories = [row['category'] for row in fetch_all("SELECT DISTINCT category FROM hackathons ORDER BY category ASC;")]
    all_hackathons = fetch_all("SELECT * FROM hackathons ORDER BY start_time ASC;")

    filtered = []
    for h in all_hackathons:
        h['status'] = get_hackathon_status(h['start_time'], h['end_time'])
        if selected_statuses and h['status'] not in selected_statuses:
            continue
        if selected_categories and h['category'] not in selected_categories:
            continue
        filtered.append(h)

    return render_template('hackathons/index.html', hackathons=filtered, categories=categories,
                           selected_statuses=selected_statuses, selected_categories=selected_categories)


@app.route('/hackathons/<int:hackathon_id>')
def hackathon_detail(hackathon_id):
    hackathon = fetch_one("SELECT * FROM hackathons WHERE id = %s;", (hackathon_id,))
    if not hackathon:
        flash('Hackathon not found.', 'danger')
        return redirect(url_for('browse_hackathons'))

    hackathon['status'] = get_hackathon_status(hackathon['start_time'], hackathon['end_time'])

    user_registration = None
    if 'user_id' in session:
        uid = session['user_id']
        user_registration = fetch_one("""
            SELECT t.id as team_id, t.leader_id,
                   CASE WHEN t.leader_id = %s THEN 'team_leader' ELSE 'team_member' END as participation_type
            FROM teams t
            LEFT JOIN team_members tm ON t.id = tm.team_id
            WHERE t.hackathon_id = %s AND (t.leader_id = %s OR (tm.user_id = %s AND tm.status = 'accepted'));
        """, (uid, hackathon_id, uid, uid))

    teams = fetch_all("""
        SELECT t.*, u.full_name as leader_name,
               (SELECT COUNT(*) FROM team_members tm WHERE tm.team_id = t.id AND tm.status = 'accepted') as member_count
        FROM teams t
        JOIN users u ON t.leader_id = u.id
        WHERE t.hackathon_id = %s
        ORDER BY t.created_at DESC;
    """, (hackathon_id,))

    if 'user_id' in session:
        uid = session['user_id']
        for t in teams:
            t['is_member'] = (t['leader_id'] == uid) or bool(
                fetch_one("SELECT id FROM team_members WHERE team_id = %s AND user_id = %s AND status = 'accepted';", (t['id'], uid))
            )

    return render_template('hackathons/detail.html', hackathon=hackathon, user_registration=user_registration, teams=teams)


@app.route('/hackathons/<int:hackathon_id>/register', methods=['GET', 'POST'])
@login_required
def register_hackathon(hackathon_id):
    hackathon = fetch_one("SELECT * FROM hackathons WHERE id = %s;", (hackathon_id,))
    if not hackathon:
        flash('Hackathon not found.', 'danger')
        return redirect(url_for('browse_hackathons'))

    if session.get('user_role') == 'admin':
        flash('Administrators cannot register as hackathon participants.', 'warning')
        return redirect(url_for('hackathon_detail', hackathon_id=hackathon_id))

    if get_hackathon_status(hackathon['start_time'], hackathon['end_time']) == 'Ended':
        flash('Registration is closed for this hackathon.', 'warning')
        return redirect(url_for('hackathon_detail', hackathon_id=hackathon_id))

    user = fetch_one("SELECT * FROM users WHERE id = %s;", (session['user_id'],))
    user_age = user.get('age') if (user and user.get('age') is not None) else 18
    min_age, max_age = hackathon.get('min_age') or 18, hackathon.get('max_age') or 100

    if user_age < 18:
        flash('Registration failed: You must be at least 18 years old (18+) to register.', 'danger')
        return redirect(url_for('hackathon_detail', hackathon_id=hackathon_id))

    if user_age < min_age or user_age > max_age:
        flash(f'Ineligible for registration: This hackathon requires participants to be between {min_age} and {max_age} years old (Your age: {user_age}).', 'danger')
        return redirect(url_for('hackathon_detail', hackathon_id=hackathon_id))

    existing_reg = fetch_one("""
        SELECT t.id as team_id FROM teams t
        LEFT JOIN team_members tm ON t.id = tm.team_id
        WHERE t.hackathon_id = %s AND (t.leader_id = %s OR (tm.user_id = %s AND tm.status = 'accepted'));
    """, (hackathon_id, session['user_id'], session['user_id']))

    if request.method == 'POST':
        p_type = request.form.get('participation_type', 'individual')
        uid = session['user_id']

        with get_db() as (conn, cur):
            if p_type == 'individual':
                cur.execute("""
                    INSERT INTO teams (hackathon_id, leader_id, team_name, description, max_members)
                    VALUES (%s, %s, %s, 'Individual Participant', 1) RETURNING id;
                """, (hackathon_id, uid, f"{session['user_name']}'s Team"))
                team_id = cur.fetchone()['id']
                cur.execute("""
                    INSERT INTO team_members (team_id, user_id, status) VALUES (%s, %s, 'accepted')
                    ON CONFLICT (team_id, user_id) DO UPDATE SET status = 'accepted';
                """, (team_id, uid))

            elif p_type == 'create_team':
                team_name = request.form.get('team_name', '').strip()
                if not team_name:
                    flash('Team name is required to create a team.', 'danger')
                    return redirect(url_for('register_hackathon', hackathon_id=hackathon_id))

                cur.execute("""
                    INSERT INTO teams (hackathon_id, leader_id, team_name, description, required_skills, max_members, whatsapp_link)
                    VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id;
                """, (
                    hackathon_id, uid, team_name, request.form.get('team_description', '').strip(),
                    request.form.get('required_skills', '').strip(), to_int(request.form.get('max_members', 4), 4),
                    request.form.get('whatsapp_link', '').strip()
                ))
                team_id = cur.fetchone()['id']
                cur.execute("""
                    INSERT INTO team_members (team_id, user_id, status) VALUES (%s, %s, 'accepted')
                    ON CONFLICT (team_id, user_id) DO UPDATE SET status = 'accepted';
                """, (team_id, uid))

            elif p_type == 'join_team' and request.form.get('team_id'):
                cur.execute("""
                    INSERT INTO team_members (team_id, user_id, status) VALUES (%s, %s, 'pending')
                    ON CONFLICT (team_id, user_id) DO NOTHING;
                """, (request.form.get('team_id'), uid))

            cur.execute("INSERT INTO notifications (user_id, title, message) VALUES (%s, %s, %s);",
                        (uid, 'Registration Confirmed', f"You have registered for {hackathon['name']}."))

        flash(f"Successfully registered for {hackathon['name']}!", 'success')
        return redirect(url_for('hackathon_detail', hackathon_id=hackathon_id))

    existing_teams = fetch_all("SELECT t.*, u.full_name as leader_name FROM teams t JOIN users u ON t.leader_id = u.id WHERE t.hackathon_id = %s;", (hackathon_id,))
    return render_template('hackathons/register.html', hackathon=hackathon, existing_teams=existing_teams, existing_reg=existing_reg)


# ==================== TEAMS & COLLABORATION ROUTES ====================

@app.route('/teams')
def browse_teams():
    search = request.args.get('search', '').strip()
    selected_h_id = request.args.get('hackathon_id', '').strip()

    hackathons = fetch_all("SELECT id, name FROM hackathons ORDER BY name ASC;")
    query = """
        SELECT t.*, h.name as hackathon_name, u.full_name as leader_name, u.institution as leader_institution,
               (SELECT COUNT(*) FROM team_members tm WHERE tm.team_id = t.id AND tm.status = 'accepted') as member_count
        FROM teams t
        JOIN hackathons h ON t.hackathon_id = h.id
        JOIN users u ON t.leader_id = u.id
        WHERE 1=1
    """
    params = []
    if selected_h_id:
        query += " AND t.hackathon_id = %s"
        params.append(selected_h_id)
    if search:
        query += " AND (t.team_name ILIKE %s OR t.required_skills ILIKE %s OR t.description ILIKE %s OR u.full_name ILIKE %s)"
        sp = f"%{search}%"
        params.extend([sp, sp, sp, sp])

    query += " ORDER BY t.created_at DESC;"
    teams = fetch_all(query, tuple(params))
    return render_template('teams/index.html', teams=teams, hackathons=hackathons, search_query=search, selected_hackathon_id=selected_h_id)


@app.route('/teams/<int:team_id>')
def manage_team(team_id):
    return view_team_details(team_id)


@app.route('/teams/<int:team_id>/details')
def view_team_details(team_id):
    team = fetch_one("""
        SELECT t.*, h.name as hackathon_name, u.full_name as leader_name, u.institution as leader_institution, u.skills as leader_skills
        FROM teams t
        JOIN hackathons h ON t.hackathon_id = h.id
        JOIN users u ON t.leader_id = u.id
        WHERE t.id = %s;
    """, (team_id,))
    if not team:
        flash('Team not found.', 'danger')
        return redirect(url_for('browse_teams'))

    user_id = session.get('user_id')
    is_leader = (user_id == team['leader_id']) if user_id else False

    is_accepted_member = is_leader
    is_invited_member = False
    if user_id and not is_accepted_member:
        mem = fetch_one("SELECT status FROM team_members WHERE team_id = %s AND user_id = %s;", (team_id, user_id))
        if mem:
            is_accepted_member = (mem['status'] == 'accepted')
            is_invited_member = (mem['status'] == 'invited')

    accepted_members = fetch_all("""
        SELECT u.id as user_id, u.full_name, u.institution, u.skills, u.github, u.linkedin, u.passport_pfp
        FROM team_members tm
        JOIN users u ON tm.user_id = u.id
        WHERE tm.team_id = %s AND tm.status = 'accepted';
    """, (team_id,))

    pending_requests, outgoing_invites, available_users = [], [], []
    invite_search = request.args.get('invite_search', '').strip()

    if is_leader:
        pending_requests = fetch_all("""
            SELECT tm.id as request_id, u.id as user_id, u.full_name, u.institution, u.skills, u.passport_pfp
            FROM team_members tm JOIN users u ON tm.user_id = u.id
            WHERE tm.team_id = %s AND tm.status = 'pending';
        """, (team_id,))

        outgoing_invites = fetch_all("""
            SELECT tm.id as invite_id, u.id as user_id, u.full_name, u.institution, u.skills, u.passport_pfp
            FROM team_members tm JOIN users u ON tm.user_id = u.id
            WHERE tm.team_id = %s AND tm.status = 'invited';
        """, (team_id,))

        user_query = """
            SELECT u.id, u.full_name, u.institution, u.skills, u.passport_pfp
            FROM users u
            WHERE u.role = 'user' AND u.id != %s
              AND u.id NOT IN (
                  SELECT tm.user_id FROM team_members tm JOIN teams t2 ON tm.team_id = t2.id
                  WHERE t2.hackathon_id = %s AND tm.status = 'accepted'
              )
              AND u.id NOT IN (SELECT tm.user_id FROM team_members tm WHERE tm.team_id = %s)
        """
        params = [user_id, team['hackathon_id'], team_id]
        if invite_search:
            user_query += " AND (u.full_name ILIKE %s OR u.skills ILIKE %s OR u.institution ILIKE %s)"
            sp = f"%{invite_search}%"
            params.extend([sp, sp, sp])
        user_query += " ORDER BY u.full_name ASC LIMIT 10;"
        available_users = fetch_all(user_query, tuple(params))

    return render_template(
        'teams/detail.html', team=team, is_leader=is_leader,
        is_accepted_member=is_accepted_member, is_invited_member=is_invited_member,
        accepted_members=accepted_members, pending_requests=pending_requests,
        outgoing_invites=outgoing_invites, available_users=available_users, invite_search=invite_search
    )


@app.route('/teams/<int:team_id>/invite/<int:user_id>', methods=['POST'])
@login_required
def invite_team_member(team_id, user_id):
    target = request.form.get('next') or request.args.get('next') or url_for('manage_team', team_id=team_id)
    if not target.startswith('/'):
        target = url_for('manage_team', team_id=team_id)

    team = fetch_one("SELECT t.*, h.name as hackathon_name FROM teams t JOIN hackathons h ON t.hackathon_id = h.id WHERE t.id = %s;", (team_id,))
    if not team or team['leader_id'] != session['user_id']:
        flash('Unauthorized permission.', 'danger')
        return redirect(target)

    member_count = fetch_one("SELECT COUNT(*) as count FROM team_members WHERE team_id = %s AND status = 'accepted';", (team_id,))['count']
    if member_count >= team['max_members']:
        flash('Team is already at maximum member capacity.', 'warning')
        return redirect(target)

    if fetch_one("""
        SELECT tm.id FROM team_members tm JOIN teams t ON tm.team_id = t.id
        WHERE t.hackathon_id = %s AND tm.user_id = %s AND tm.status = 'accepted';
    """, (team['hackathon_id'], user_id)):
        flash('This user is already part of a team in this hackathon.', 'warning')
        return redirect(target)

    execute_sql("""
        INSERT INTO team_members (team_id, user_id, status, requested_at, updated_at)
        VALUES (%s, %s, 'invited', NOW(), NOW())
        ON CONFLICT (team_id, user_id) DO UPDATE SET status = 'invited', updated_at = NOW();
    """, (team_id, user_id))

    invited_user = fetch_one("SELECT full_name FROM users WHERE id = %s;", (user_id,))
    execute_sql("INSERT INTO notifications (user_id, title, message) VALUES (%s, %s, %s);",
                (user_id, 'Team Invitation', f"You have been invited by {session['user_name']} to join '{team['team_name']}' for {team['hackathon_name']}!"))

    flash(f"Invitation sent to {invited_user['full_name'] if invited_user else 'user'} for {team['team_name']}!", 'success')
    return redirect(target)


@app.route('/teams/<int:team_id>/cancel-invite/<int:user_id>', methods=['POST'])
@login_required
def cancel_team_invite(team_id, user_id):
    target = request.form.get('next') or request.args.get('next') or url_for('manage_team', team_id=team_id)
    team = fetch_one("SELECT leader_id FROM teams WHERE id = %s;", (team_id,))
    if not team or team['leader_id'] != session['user_id']:
        flash('Unauthorized permission.', 'danger')
        return redirect(url_for('browse_teams'))

    execute_sql("DELETE FROM team_members WHERE team_id = %s AND user_id = %s AND status = 'invited';", (team_id, user_id))
    flash('Invitation cancelled.', 'info')
    return redirect(target)


@app.route('/teams/invitation/<int:team_id>/<action>', methods=['POST'])
@login_required
def respond_team_invitation(team_id, action):
    if action not in ['accept', 'decline']:
        flash('Invalid action.', 'danger')
        return redirect(url_for('view_profile'))

    membership = fetch_one("""
        SELECT tm.*, t.team_name, t.hackathon_id, t.leader_id, t.max_members
        FROM team_members tm JOIN teams t ON tm.team_id = t.id
        WHERE tm.team_id = %s AND tm.user_id = %s AND tm.status = 'invited';
    """, (team_id, session['user_id']))

    if not membership:
        flash('No active invitation found.', 'warning')
        return redirect(url_for('view_profile'))

    if action == 'accept':
        cur_count = fetch_one("SELECT COUNT(*) as count FROM team_members WHERE team_id = %s AND status = 'accepted';", (team_id,))['count']
        if cur_count >= membership['max_members']:
            flash('Sorry, this team is already full.', 'warning')
            return redirect(url_for('view_profile'))

        if fetch_one("""
            SELECT t.id FROM teams t JOIN team_members tm ON t.id = tm.team_id
            WHERE t.hackathon_id = %s AND tm.user_id = %s AND tm.status = 'accepted';
        """, (membership['hackathon_id'], session['user_id'])):
            flash('You are already part of another team in this event.', 'warning')
            return redirect(url_for('view_profile'))

        execute_sql("UPDATE team_members SET status = 'accepted', updated_at = NOW() WHERE team_id = %s AND user_id = %s;", (team_id, session['user_id']))
        execute_sql("INSERT INTO notifications (user_id, title, message) VALUES (%s, %s, %s);",
                    (membership['leader_id'], 'Invitation Accepted', f"{session['user_name']} accepted your invitation and joined '{membership['team_name']}'!"))
        flash(f"You have joined '{membership['team_name']}'!", 'success')
        return redirect(url_for('manage_team', team_id=team_id))
    else:
        execute_sql("DELETE FROM team_members WHERE team_id = %s AND user_id = %s AND status = 'invited';", (team_id, session['user_id']))
        execute_sql("INSERT INTO notifications (user_id, title, message) VALUES (%s, %s, %s);",
                    (membership['leader_id'], 'Invitation Declined', f"{session['user_name']} declined the invitation to join '{membership['team_name']}'."))
        flash('Invitation declined.', 'info')
        return redirect(url_for('view_profile'))


# ==================== COMMUNITY & PARTICIPANT DIRECTORY ====================

@app.route('/users')
def browse_users():
    search = request.args.get('search', '').strip()
    skill_filter = request.args.get('skill', '').strip()

    query = """
        SELECT u.id, u.full_name, u.institution, u.skills, u.github, u.linkedin, u.passport_pfp, u.role, u.created_at,
               (SELECT COUNT(*) FROM team_members tm WHERE tm.user_id = u.id AND tm.status = 'accepted') as total_events
        FROM users u WHERE u.role = 'user'
    """
    params = []
    if search:
        query += " AND (u.full_name ILIKE %s OR u.institution ILIKE %s OR u.skills ILIKE %s)"
        sp = f"%{search}%"
        params.extend([sp, sp, sp])
    if skill_filter:
        query += " AND u.skills ILIKE %s"
        params.append(f"%{skill_filter}%")

    query += " ORDER BY u.full_name ASC;"
    users = fetch_all(query, tuple(params))

    leader_teams, user_team_status, user_hackathon_taken = [], {}, {}
    if session.get('user_id') and session.get('user_role') != 'admin':
        leader_teams = fetch_all("""
            SELECT t.id as team_id, t.team_name, t.hackathon_id, h.name as hackathon_name, t.max_members,
                   (SELECT COUNT(*) FROM team_members tm WHERE tm.team_id = t.id AND tm.status = 'accepted') as member_count
            FROM teams t JOIN hackathons h ON t.hackathon_id = h.id
            WHERE t.leader_id = %s ORDER BY t.created_at DESC;
        """, (session['user_id'],))

        if leader_teams:
            team_ids = [t['team_id'] for t in leader_teams]
            hackathon_ids = [t['hackathon_id'] for t in leader_teams]

            for tm in fetch_all("SELECT team_id, user_id, status FROM team_members WHERE team_id = ANY(%s);", (team_ids,)):
                user_team_status[(tm['user_id'], tm['team_id'])] = tm['status']

            for acc in fetch_all("""
                SELECT tm.user_id, t.hackathon_id FROM team_members tm JOIN teams t ON tm.team_id = t.id
                WHERE t.hackathon_id = ANY(%s) AND tm.status = 'accepted';
            """, (hackathon_ids,)):
                user_hackathon_taken[(acc['user_id'], acc['hackathon_id'])] = True

    return render_template('users/index.html', users=users, search_query=search, skill_filter=skill_filter,
                           leader_teams=leader_teams, user_team_status=user_team_status, user_hackathon_taken=user_hackathon_taken)


@app.route('/users/<int:user_id>')
def public_profile(user_id):
    user = fetch_one("SELECT * FROM users WHERE id = %s;", (user_id,))
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('browse_users'))

    account = fetch_one("SELECT * FROM accounts WHERE user_id = %s;", (user_id,))
    user_teams = fetch_all("""
        SELECT DISTINCT h.id as hackathon_id, h.name as hackathon_name, h.category, h.start_time, h.end_time,
               t.id as team_id, t.team_name, t.description as team_desc,
               CASE WHEN t.leader_id = %s THEN 'Team Leader' ELSE 'Team Member' END as member_role
        FROM teams t JOIN hackathons h ON t.hackathon_id = h.id
        LEFT JOIN team_members tm ON t.id = tm.team_id AND tm.user_id = %s
        WHERE t.leader_id = %s OR (tm.user_id = %s AND tm.status = 'accepted')
        ORDER BY h.start_time DESC;
    """, (user_id, user_id, user_id, user_id))

    for t in user_teams:
        t['hackathon_status'] = get_hackathon_status(t['start_time'], t['end_time'])

    user_projects = fetch_all("""
        SELECT p.*, h.name as hackathon_name, t.team_name FROM projects p
        JOIN hackathons h ON p.hackathon_id = h.id LEFT JOIN teams t ON p.team_id = t.id
        WHERE p.user_id = %s ORDER BY p.submitted_at DESC;
    """, (user_id,))

    return render_template('users/profile.html', user=user, account=account, user_teams=user_teams, user_projects=user_projects)


# ==================== TEAM INTERACTIONS & PROJECT SUBMISSION ====================

@app.route('/teams/<int:team_id>/request-join', methods=['POST'])
@login_required
def request_join_team(team_id):
    if session.get('user_role') == 'admin':
        flash('Administrators cannot join teams.', 'warning')
        return redirect(url_for('view_team_details', team_id=team_id))

    team = fetch_one("SELECT * FROM teams WHERE id = %s;", (team_id,))
    if not team:
        flash('Team not found.', 'danger')
        return redirect(url_for('browse_hackathons'))

    if fetch_one("""
        SELECT t.id FROM teams t LEFT JOIN team_members tm ON t.id = tm.team_id
        WHERE t.hackathon_id = %s AND (t.leader_id = %s OR (tm.user_id = %s AND tm.status = 'accepted'));
    """, (team['hackathon_id'], session['user_id'], session['user_id'])):
        flash('You are already part of a team in this hackathon.', 'warning')
        return redirect(url_for('hackathon_detail', hackathon_id=team['hackathon_id']))

    execute_sql("""
        INSERT INTO team_members (team_id, user_id, status) VALUES (%s, %s, 'pending')
        ON CONFLICT (team_id, user_id) DO UPDATE SET status = 'pending';
    """, (team_id, session['user_id']))

    execute_sql("INSERT INTO notifications (user_id, title, message) VALUES (%s, %s, %s);",
                (team['leader_id'], 'New Team Join Request', f"{session['user_name']} requested to join '{team['team_name']}'."))

    flash('Join request sent to the team leader!', 'success')
    return redirect(url_for('hackathon_detail', hackathon_id=team['hackathon_id']))


@app.route('/teams/request/<int:request_id>/<action>', methods=['POST'])
@login_required
def respond_join_request(request_id, action):
    if action not in ['accept', 'reject']:
        flash('Invalid action.', 'danger')
        return redirect(url_for('browse_hackathons'))

    tm_req = fetch_one("SELECT tm.*, t.leader_id, t.team_name, t.hackathon_id FROM team_members tm JOIN teams t ON tm.team_id = t.id WHERE tm.id = %s;", (request_id,))
    if not tm_req or tm_req['leader_id'] != session['user_id']:
        flash('Unauthorized permission.', 'danger')
        return redirect(url_for('browse_hackathons'))

    new_status = 'accepted' if action == 'accept' else 'rejected'
    execute_sql("UPDATE team_members SET status = %s, updated_at = NOW() WHERE id = %s;", (new_status, request_id))
    execute_sql("INSERT INTO notifications (user_id, title, message) VALUES (%s, %s, %s);",
                (tm_req['user_id'], 'Team Request Update', f"Join request for team '{tm_req['team_name']}' was {new_status}!"))

    flash(f"Request {action}ed successfully.", 'success')
    return redirect(url_for('manage_team', team_id=tm_req['team_id']))


@app.route('/teams/<int:team_id>/remove/<int:user_id>', methods=['POST'])
@login_required
def remove_team_member(team_id, user_id):
    team = fetch_one("SELECT leader_id FROM teams WHERE id = %s;", (team_id,))
    if not team or team['leader_id'] != session['user_id']:
        flash('Unauthorized permission.', 'danger')
        return redirect(url_for('browse_hackathons'))

    execute_sql("DELETE FROM team_members WHERE team_id = %s AND user_id = %s;", (team_id, user_id))
    flash('Member removed from team.', 'info')
    return redirect(url_for('manage_team', team_id=team_id))


@app.route('/teams/<int:team_id>/update', methods=['POST'])
@login_required
def update_team_info(team_id):
    team = fetch_one("SELECT leader_id FROM teams WHERE id = %s;", (team_id,))
    if not team or team['leader_id'] != session['user_id']:
        flash('Unauthorized permission.', 'danger')
        return redirect(url_for('browse_hackathons'))

    execute_sql("""
        UPDATE teams 
        SET team_name = %s, description = %s, required_skills = %s, whatsapp_link = %s
        WHERE id = %s;
    """, (request.form.get('team_name', '').strip(), request.form.get('description', '').strip(),
          request.form.get('required_skills', '').strip(), request.form.get('whatsapp_link', '').strip(), team_id))

    flash('Team information updated successfully!', 'success')
    return redirect(url_for('manage_team', team_id=team_id))


@app.route('/hackathons/<int:hackathon_id>/submit', methods=['GET', 'POST'])
@login_required
def submit_project(hackathon_id):
    if session.get('user_role') == 'admin':
        flash('Administrators cannot submit projects.', 'warning')
        return redirect(url_for('hackathon_detail', hackathon_id=hackathon_id))

    hackathon = fetch_one("SELECT * FROM hackathons WHERE id = %s;", (hackathon_id,))
    if not hackathon:
        flash('Hackathon not found.', 'danger')
        return redirect(url_for('browse_hackathons'))

    if get_hackathon_status(hackathon['start_time'], hackathon['end_time']) == 'Ended':
        flash('Project submission is closed.', 'danger')
        return redirect(url_for('hackathon_detail', hackathon_id=hackathon_id))

    user_team = fetch_one("""
        SELECT t.id as team_id FROM teams t LEFT JOIN team_members tm ON t.id = tm.team_id
        WHERE t.hackathon_id = %s AND (t.leader_id = %s OR (tm.user_id = %s AND tm.status = 'accepted'));
    """, (hackathon_id, session['user_id'], session['user_id']))

    if not user_team:
        flash('You must be registered in a team to submit a project.', 'warning')
        return redirect(url_for('hackathon_detail', hackathon_id=hackathon_id))

    existing_project = fetch_one("SELECT * FROM projects WHERE hackathon_id = %s AND (user_id = %s OR team_id = %s);",
                                 (hackathon_id, session['user_id'], user_team['team_id']))

    if request.method == 'POST':
        p_name = request.form.get('project_name', '').strip()
        desc = request.form.get('description', '').strip()
        tech = request.form.get('technologies_used', '').strip()
        github = request.form.get('github_link', '').strip()
        demo = request.form.get('demo_link', '').strip()

        if existing_project:
            execute_sql("""
                UPDATE projects SET project_name = %s, description = %s, technologies_used = %s, github_link = %s, demo_link = %s, submitted_at = NOW()
                WHERE id = %s;
            """, (p_name, desc, tech, github, demo, existing_project['id']))
            flash('Project submission updated successfully!', 'success')
        else:
            execute_sql("""
                INSERT INTO projects (hackathon_id, team_id, user_id, project_name, description, technologies_used, github_link, demo_link)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
            """, (hackathon_id, user_team['team_id'], session['user_id'], p_name, desc, tech, github, demo))
            flash('Project submitted successfully!', 'success')

        return redirect(url_for('hackathon_detail', hackathon_id=hackathon_id))

    return render_template('projects/submit.html', hackathon=hackathon, existing_project=existing_project)


# ==================== NOTIFICATIONS ====================

@app.route('/notifications')
@login_required
def view_notifications():
    notifications = fetch_all("SELECT * FROM notifications WHERE user_id = %s OR user_id IS NULL ORDER BY created_at DESC;", (session['user_id'],))
    execute_sql("UPDATE notifications SET is_read = TRUE WHERE user_id = %s OR user_id IS NULL;", (session['user_id'],))
    return render_template('notifications.html', notifications=notifications)


@app.route('/notifications/mark-read', methods=['POST'])
@login_required
def mark_all_notifications_read():
    execute_sql("UPDATE notifications SET is_read = TRUE WHERE user_id = %s OR user_id IS NULL;", (session['user_id'],))
    flash('All notifications marked as read.', 'info')
    return redirect(url_for('view_notifications'))


# ==================== ADMIN CONTROL CENTER & ANALYTICS ====================

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        admin_user = fetch_one("SELECT * FROM users WHERE (email = %s OR email = %s) AND role = 'admin';", (username.lower(), Config.ADMIN_EMAIL))
        if admin_user and (check_password_hash(admin_user['password_hash'], password) or (username == Config.ADMIN_USERNAME and password == Config.ADMIN_PASSWORD)):
            session.permanent = True
            session['user_id'] = admin_user['id']
            session['user_name'] = admin_user['full_name']
            session['user_email'] = admin_user['email']
            session['user_role'] = 'admin'
            if session.get('visitor_session'):
                try:
                    execute_sql("UPDATE site_visitors SET user_id = %s, last_activity = NOW() WHERE session_id = %s;",
                                (admin_user['id'], session['visitor_session']))
                except Exception:
                    pass
            flash('Admin authentication successful.', 'success')
            return redirect(url_for('admin_dashboard'))

        flash('Invalid administrator credentials.', 'danger')

    return render_template('admin/login.html')


@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    total_users = fetch_one("SELECT COUNT(*) as count FROM users WHERE role = 'user';")['count']
    total_hackathons = fetch_one("SELECT COUNT(*) as count FROM hackathons;")['count']
    total_teams = fetch_one("SELECT COUNT(*) as count FROM teams;")['count']
    total_projects = fetch_one("SELECT COUNT(*) as count FROM projects;")['count']

    # Active registered users in last 30 minutes
    active_users = fetch_one("""
        SELECT COUNT(DISTINCT sv.user_id) as count FROM site_visitors sv JOIN users u ON sv.user_id = u.id 
        WHERE sv.user_id IS NOT NULL AND u.role = 'user' AND sv.last_activity >= NOW() - INTERVAL '30 minutes';
    """)['count']
    active_users = min(max(active_users, 1 if total_users > 0 else 0), total_users)

    # Visitor sessions
    guest_visitors = fetch_one("""
        SELECT COUNT(DISTINCT session_id) as count FROM site_visitors 
        WHERE user_id IS NULL AND last_activity >= NOW() - INTERVAL '24 hours';
    """)['count']
    total_visitors = max(total_users + min(guest_visitors, 2), 1)

    stats = {
        'total_users': total_users, 'total_visitors': total_visitors, 'active_users': active_users,
        'total_hackathons': total_hackathons, 'total_teams': total_teams, 'total_projects': total_projects
    }
    return render_template('admin/dashboard.html', stats=stats)


# ==================== ADMIN TEAMS & PROJECTS SUPERVISION ====================

@app.route('/admin/teams')
@admin_required
def admin_teams():
    search = request.args.get('search', '').strip()
    selected_h_id = request.args.get('hackathon_id', '').strip()
    hackathons = fetch_all("SELECT id, name FROM hackathons ORDER BY name ASC;")
    
    query = """
        SELECT t.*, 
               h.name as hackathon_name, h.category as hackathon_category, h.start_time, h.end_time,
               u.full_name as leader_name, u.email as leader_email, u.phone as leader_phone, 
               u.institution as leader_institution, u.skills as leader_skills, u.passport_pfp as leader_pfp,
               (SELECT COUNT(*) FROM team_members tm WHERE tm.team_id = t.id AND tm.status = 'accepted') as member_count,
               p.id as project_id, p.project_name, p.description as project_description, 
               p.technologies_used, p.github_link, p.demo_link, p.submitted_at as project_submitted_at
        FROM teams t
        JOIN hackathons h ON t.hackathon_id = h.id
        JOIN users u ON t.leader_id = u.id
        LEFT JOIN projects p ON (p.team_id = t.id OR (p.hackathon_id = t.hackathon_id AND p.user_id = t.leader_id))
        WHERE 1=1
    """
    params = []
    if selected_h_id:
        query += " AND t.hackathon_id = %s"
        params.append(selected_h_id)
    if search:
        query += " AND (t.team_name ILIKE %s OR u.full_name ILIKE %s OR h.name ILIKE %s OR p.project_name ILIKE %s)"
        sp = f"%{search}%"
        params.extend([sp, sp, sp, sp])

    query += " ORDER BY t.created_at DESC;"
    teams = fetch_all(query, tuple(params))

    for team in teams:
        team['hackathon_status'] = get_hackathon_status(team['start_time'], team['end_time'])
        team['members'] = fetch_all("""
            SELECT u.id as user_id, u.full_name, u.email, u.phone, u.institution, u.skills, u.passport_pfp,
                   tm.requested_at, (u.id = %s) as is_leader
            FROM team_members tm JOIN users u ON tm.user_id = u.id
            WHERE tm.team_id = %s AND tm.status = 'accepted'
            ORDER BY is_leader DESC, u.full_name ASC;
        """, (team['leader_id'], team['id']))

    team_stats = {
        'total_teams': len(teams),
        'with_projects': sum(1 for t in teams if t.get('project_id')),
        'total_members': sum(t.get('member_count', 0) for t in teams)
    }

    return render_template('admin/teams.html', teams=teams, hackathons=hackathons,
                           search_query=search, selected_hackathon_id=selected_h_id, team_stats=team_stats)


@app.route('/admin/teams/<int:team_id>/delete', methods=['POST'])
@admin_required
def admin_delete_team(team_id):
    team = fetch_one("SELECT team_name FROM teams WHERE id = %s;", (team_id,))
    if not team:
        flash('Team not found.', 'danger')
        return redirect(url_for('admin_teams'))

    execute_sql("DELETE FROM teams WHERE id = %s;", (team_id,))
    flash(f"Team '{team['team_name']}' and its project records have been deleted.", 'info')
    return redirect(url_for('admin_teams'))


@app.route('/admin/teams/<int:team_id>/kick/<int:user_id>', methods=['POST'])
@admin_required
def admin_kick_team_member(team_id, user_id):
    team = fetch_one("SELECT * FROM teams WHERE id = %s;", (team_id,))
    if not team:
        flash('Team not found.', 'danger')
        return redirect(url_for('admin_teams'))

    user = fetch_one("SELECT full_name FROM users WHERE id = %s;", (user_id,))
    u_name = user['full_name'] if user else 'User'

    if team['leader_id'] == user_id:
        other_member = fetch_one("SELECT user_id FROM team_members WHERE team_id = %s AND user_id != %s AND status = 'accepted' LIMIT 1;", (team_id, user_id))
        if other_member:
            execute_sql("UPDATE teams SET leader_id = %s WHERE id = %s;", (other_member['user_id'], team_id))
            execute_sql("DELETE FROM team_members WHERE team_id = %s AND user_id = %s;", (team_id, user_id))
            flash(f"Team leader {u_name} was removed. Leadership transferred to next team member.", 'warning')
        else:
            execute_sql("DELETE FROM teams WHERE id = %s;", (team_id,))
            flash(f"Team '{team['team_name']}' was disbanded as the leader {u_name} was removed.", 'info')
    else:
        execute_sql("DELETE FROM team_members WHERE team_id = %s AND user_id = %s;", (team_id, user_id))
        flash(f"Member '{u_name}' removed from team '{team['team_name']}'.", 'info')

    return redirect(url_for('admin_teams'))


# ==================== ADMIN HACKATHON CRUD ====================

@app.route('/admin/hackathons', methods=['GET'])
@admin_required
def admin_hackathons():
    edit_id = request.args.get('edit_id')
    edit_hackathon = fetch_one("SELECT * FROM hackathons WHERE id = %s;", (edit_id,)) if edit_id else None
    hackathons = fetch_all("SELECT * FROM hackathons ORDER BY start_time DESC;")
    for h in hackathons:
        h['status'] = get_hackathon_status(h['start_time'], h['end_time'])

    return render_template('admin/hackathons.html', hackathons=hackathons, edit_hackathon=edit_hackathon)


@app.route('/admin/hackathons/add', methods=['POST'])
@admin_required
def admin_add_hackathon():
    execute_sql("""
        INSERT INTO hackathons (name, category, logo_url, short_description, full_description, min_age, max_age, start_time, end_time)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
    """, (
        request.form.get('name', '').strip(), request.form.get('category', '').strip(),
        request.form.get('logo_url', '🚀').strip(), request.form.get('short_description', '').strip(),
        request.form.get('full_description', '').strip(),
        to_int(request.form.get('min_age', 18), 18), to_int(request.form.get('max_age', 100), 100),
        request.form.get('start_time'), request.form.get('end_time')
    ))
    flash('Hackathon created successfully!', 'success')
    return redirect(url_for('admin_hackathons'))


@app.route('/admin/hackathons/<int:hackathon_id>/edit', methods=['POST'])
@admin_required
def admin_edit_hackathon(hackathon_id):
    execute_sql("""
        UPDATE hackathons 
        SET name = %s, category = %s, logo_url = %s, short_description = %s, full_description = %s, min_age = %s, max_age = %s, start_time = %s, end_time = %s
        WHERE id = %s;
    """, (
        request.form.get('name', '').strip(), request.form.get('category', '').strip(),
        request.form.get('logo_url', '🚀').strip(), request.form.get('short_description', '').strip(),
        request.form.get('full_description', '').strip(),
        to_int(request.form.get('min_age', 18), 18), to_int(request.form.get('max_age', 100), 100),
        request.form.get('start_time'), request.form.get('end_time'), hackathon_id
    ))
    flash('Hackathon updated successfully!', 'success')
    return redirect(url_for('admin_hackathons'))


@app.route('/admin/hackathons/<int:hackathon_id>/delete', methods=['POST'])
@admin_required
def admin_delete_hackathon(hackathon_id):
    execute_sql("DELETE FROM hackathons WHERE id = %s;", (hackathon_id,))
    flash('Hackathon deleted.', 'info')
    return redirect(url_for('admin_hackathons'))


# ==================== ADMIN USER & AUDIT MANAGEMENT ====================

@app.route('/admin/users')
@admin_required
def admin_users():
    search = request.args.get('search', '').strip()
    filter_role = request.args.get('role', 'all').strip().lower()
    filter_status = request.args.get('status', 'all').strip().lower()

    query = """
        SELECT u.*, a.id as account_id,
               (SELECT COUNT(*) FROM team_members tm WHERE tm.user_id = u.id AND tm.status = 'accepted') as registration_count,
               (SELECT COUNT(*) FROM teams t WHERE t.leader_id = u.id) as teams_led_count,
               (SELECT MAX(sv.last_activity) FROM site_visitors sv WHERE sv.user_id = u.id) as last_seen
        FROM users u LEFT JOIN accounts a ON a.user_id = u.id WHERE 1=1
    """
    params = []
    if search:
        query += " AND (u.full_name ILIKE %s OR u.email ILIKE %s OR u.institution ILIKE %s OR u.skills ILIKE %s OR u.phone ILIKE %s)"
        sp = f"%{search}%"
        params.extend([sp, sp, sp, sp, sp])

    if filter_role in ['user', 'admin']:
        query += " AND u.role = %s"
        params.append(filter_role)

    if filter_status == 'locked':
        query += " AND u.lock_until IS NOT NULL AND u.lock_until > NOW()"
    elif filter_status == 'active':
        query += " AND (u.lock_until IS NULL OR u.lock_until <= NOW())"

    query += " ORDER BY u.created_at DESC;"
    users = fetch_all(query, tuple(params))

    now_utc = datetime.now(timezone.utc)
    for u in users:
        last_s = u.get('last_seen')
        if last_s:
            if last_s.tzinfo is None:
                last_s = last_s.replace(tzinfo=timezone.utc)
            u['is_online'] = (now_utc - last_s).total_seconds() <= 1800
        else:
            u['is_online'] = False

        lock_u = u.get('lock_until')
        if lock_u:
            if lock_u.tzinfo is None:
                lock_u = lock_u.replace(tzinfo=timezone.utc)
            u['is_locked'] = now_utc < lock_u
            if u['is_locked']:
                u['lock_remaining_seconds'] = int((lock_u - now_utc).total_seconds())
        else:
            u['is_locked'] = False

    if filter_status == 'online':
        users = [u for u in users if u.get('is_online')]

    user_stats = {
        'total': fetch_one("SELECT COUNT(*) as c FROM users;")['c'],
        'regular_users': fetch_one("SELECT COUNT(*) as c FROM users WHERE role = 'user';")['c'],
        'admins': fetch_one("SELECT COUNT(*) as c FROM users WHERE role = 'admin';")['c'],
        'locked': fetch_one("SELECT COUNT(*) as c FROM users WHERE lock_until IS NOT NULL AND lock_until > NOW();")['c'],
        'with_docs': fetch_one("SELECT COUNT(*) as c FROM users WHERE birth_certificate IS NOT NULL OR aadhaar_card IS NOT NULL;")['c']
    }

    return render_template('admin/users.html', users=users, user_stats=user_stats, search_query=search,
                           filter_role=filter_role, filter_status=filter_status)


@app.route('/admin/users/<int:user_id>/unlock', methods=['POST'])
@admin_required
def admin_unlock_user(user_id):
    execute_sql("UPDATE users SET failed_login_count = 0, lock_until = NULL WHERE id = %s;", (user_id,))
    flash('User account unlocked successfully.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@admin_required
def admin_delete_user(user_id):
    if user_id == session.get('user_id'):
        flash('You cannot delete your own logged-in administrator account.', 'danger')
        return redirect(url_for('admin_users'))

    user = fetch_one("SELECT * FROM users WHERE id = %s;", (user_id,))
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin_users'))

    for file_col in ['passport_pfp', 'birth_certificate', 'aadhaar_card']:
        f_url = user.get(file_col)
        if f_url and f_url.startswith('/static/'):
            try:
                abs_f = os.path.join(app.root_path, f_url.lstrip('/'))
                if os.path.exists(abs_f):
                    os.remove(abs_f)
            except Exception:
                pass

    execute_sql("DELETE FROM users WHERE id = %s;", (user_id,))
    flash(f"User '{user['full_name']}' has been permanently deleted.", 'info')
    return redirect(url_for('admin_users'))


@app.route('/admin/registrations')
@admin_required
def admin_registrations():
    search = request.args.get('search', '').strip()
    query = """
        SELECT t.id as team_id, t.team_name, t.description as team_desc, t.max_members, t.created_at as registered_at,
               h.id as hackathon_id, h.name as hackathon_name, h.category as hackathon_category,
               CASE 
                   WHEN NOW() < h.start_time THEN 'Upcoming'
                   WHEN NOW() > h.end_time THEN 'Ended'
                   ELSE 'Live'
               END as hackathon_status,
               u.id as leader_id, u.full_name as leader_name, u.email as leader_email, u.institution as leader_institution, u.passport_pfp as leader_pfp,
               (SELECT COUNT(*) FROM team_members tm WHERE tm.team_id = t.id AND tm.status = 'accepted') as member_count,
               p.id as project_id, p.project_name, p.github_link, p.demo_link
        FROM teams t
        JOIN hackathons h ON t.hackathon_id = h.id
        JOIN users u ON t.leader_id = u.id
        LEFT JOIN projects p ON p.team_id = t.id
        WHERE 1=1
    """
    params = []
    if search:
        query += " AND (t.team_name ILIKE %s OR h.name ILIKE %s OR u.full_name ILIKE %s OR u.email ILIKE %s OR u.institution ILIKE %s)"
        sp = f"%{search}%"
        params.extend([sp, sp, sp, sp, sp])

    query += " ORDER BY t.created_at DESC;"
    registrations = fetch_all(query, tuple(params))
    return render_template('admin/registrations.html', registrations=registrations, search_query=search)


@app.route('/admin/announcements', methods=['GET', 'POST'])
@admin_required
def admin_announcements():
    if request.method == 'POST':
        execute_sql("INSERT INTO notifications (user_id, title, message) VALUES (NULL, %s, %s);",
                    (request.form.get('title', '').strip(), request.form.get('message', '').strip()))
        flash('Announcement broadcasted to all users successfully!', 'success')

    announcements = fetch_all("SELECT * FROM notifications WHERE user_id IS NULL ORDER BY created_at DESC;")
    return render_template('admin/announcements.html', announcements=announcements)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    if getattr(sys, 'frozen', False):
        import webbrowser, threading
        def open_browser():
            webbrowser.open_new(f"http://127.0.0.1:{port}")
        threading.Timer(1.2, open_browser).start()
        print("==================================================================")
        print(f" Hackathon Hub is running at: http://127.0.0.1:{port}")
        print(" Browser is launching automatically...")
        print(" Press Ctrl+C in this console window to exit/stop the server.")
        print("==================================================================")
        app.run(debug=False, port=port, host='127.0.0.1')
    else:
        app.run(debug=True, port=port)
