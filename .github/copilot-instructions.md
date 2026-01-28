# DeFiTome Copilot Instructions

## Project Overview
**DeFiTome** is a Django 6.0 web application for decentralized finance operations. It's structured as a multi-app Django project with domain-specific modules handling different blockchain/DeFi functionalities.

## Architecture

### Core Structure
- **Framework**: Django 6.0.1 with SQLite3 database
- **Project Root**: `/Tome/` directory (contains settings, urls, wsgi, asgi)
- **Apps**: Six domain-specific Django apps in `/Tome/`:
  - `User`: Authentication and user profiles (has templates in `User/templates/`)
  - `Wallet`: Blockchain wallet management (crypto libraries present)
  - `DeFi`: DeFi protocol interactions
  - `Explorer`: Blockchain explorer functionality
  - `Marketplace`: DeFi marketplace/trading features
  - `Settings`: User and application settings

### Key Dependencies
- **Blockchain**: `coincurve`, `ecdsa`, `ed25519-blake2b`, `hdwallet` for cryptographic operations
- **RPC Clients**: `evrmore-rpc` for blockchain RPC communication
- **Cloud Storage**: `django-storages` and `boto3` (AWS integration for file uploads)
- **Database**: Django ORM with SQLite3 (migration system in each app's `migrations/` folder)

## URL Routing Pattern
- Root URLconf: `Tome/urls.py` includes app-specific urls via `path('user/', include('User.urls'))`
- Each app implements `urls.py` with its own route definitions
- Templates follow app structure: `{app_name}/templates/{feature}/{page}.html`

## Development Workflows

### Common Commands
```bash
# From /Tome directory
python manage.py runserver      # Development server on http://localhost:8000
python manage.py makemigrations # Create migration files
python manage.py migrate        # Apply database migrations
python manage.py createsuperuser # Create admin user
python manage.py startapp {name} # Generate new app
```

### Testing
- Test files exist as `tests.py` in each app (currently minimal/placeholder content)
- Use Django's test runner: `python manage.py test`

## Project Conventions

### Model Layer (`models.py`)
- User model in `User/models.py`: `UserWallet` with UUID primary key, entropy, passphrase, timestamps
- Models use Django's built-in `auto_now_add` and `auto_now` for timestamps
- Each model includes `__str__()` method for readable representation

### View Layer (`views.py`)
- Function-based views (not class-based) in `User/views.py`
- Simple render pattern: `render(request, 'template_path.html')`
- No request body validation yet (initial stage)

### Templates
- Located in `{app}/templates/{feature}/{page}.html`
- Inline CSS (no separate CSS files currently, style.css exists but unused)
- Modern frontend with gradient backgrounds and responsive design in User app

### Admin Interface
- Standard Django admin at `/admin/` 
- Each app has `admin.py` (currently minimal)
- Configure model admin registration as: `admin.site.register(ModelClass)`

## Critical Integration Points
- **Blockchain Wallet**: `UserWallet` model stores entropy and passphrase (handle with caution - security concern)
- **App Configuration**: `INSTALLED_APPS` in `settings.py` must include new apps
- **Static Files**: Served from `Tome/static/` (configured in `django.contrib.staticfiles`)
- **Database**: Migrations tracked per-app in `migrations/` folders (always run `migrate` after model changes)

## Security & Database Notes
- `DEBUG = True` in settings (development only)
- `SECRET_KEY` hardcoded (should use environment variables via `python-decouple`)
- Currently uses `decouple.config()` for environment variable loading but not consistently
- SQLite3 (single-file database - not suitable for production)

## Important Locations
- Settings: [Tome/settings.py](Tome/settings.py)
- Root URLs: [Tome/urls.py](Tome/urls.py)
- User authentication templates: [User/templates/](User/templates/)
- Database: `Tome/db.sqlite3` (git-ignored)

## When Adding New Features
1. Create model in app's `models.py` with proper timestamps
2. Register in app's `admin.py` for admin interface access
3. Create views in app's `views.py` (follow function-based pattern)
4. Add URL routes to app's `urls.py`
5. Create templates in `{app}/templates/{feature}/` structure
6. Run `makemigrations` and `migrate` after model changes
7. Include app in `INSTALLED_APPS` if it's a new app
