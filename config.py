import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'hackathon_hub_super_secret_key_2026_antigravity')
    PERMANENT_SESSION_LIFETIME = 604800  # 7 days in seconds

    
    # PostgreSQL Configuration
    DB_HOST = os.environ.get('DB_HOST', '127.0.0.1')
    DB_PORT = int(os.environ.get('DB_PORT', 5432))
    DB_NAME = os.environ.get('DB_NAME', 'hackathon_hub')
    DB_USER = os.environ.get('DB_USER', 'postgres')
    DB_PASSWORD = os.environ.get('DB_PASSWORD', '')

    # Admin Credentials
    ADMIN_USERNAME = 'Admin'
    ADMIN_EMAIL = 'admin@hackathonhub.com'
    ADMIN_PASSWORD = 'AdminPass123!'
