import os
import socket
import sys
import traceback
from flask import Flask, render_template, jsonify, request
from config import config
from extensions import db, login_manager, limiter, cache, socketio
from models import Issue, User
from geoalchemy2.functions import ST_DWithin, ST_GeogFromText
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from data_exporter import exporter

def create_app(config_class=config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Initialize data exporter
    exporter.init_app(app)
    
    # Force PostgreSQL to use pg8000 dialect
    if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgresql://'):
        app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace(
            'postgresql://', 'postgresql+pg8000://', 1
        )

    # Initialize extensions
    db.init_app(app)
    login_manager.init_app(app)
    
    # Initialize limiter (for development, in-memory storage is automatic)
    limiter.init_app(app)
    
    cache.init_app(app)
    socketio.init_app(app, async_mode='threading')

    # Register routes
    @app.route('/')
    def index():
        return render_template('index.html')

    @app.route('/api/issues', methods=['GET'])
    @limiter.limit("100/minute")
    def get_issues():
        try:
            lat = float(request.args.get('lat', 12.9716))
            lng = float(request.args.get('lng', 77.5946))
            radius = float(request.args.get('radius', 5)) * 1000

            base_query = db.session.query(Issue)
            
            # FIXED: Use proper GeoAlchemy2 geography casting
            base_query = base_query.filter(
                ST_DWithin(
                    Issue.location,  # Remove .cast(db.geography)
                    ST_GeogFromText(f'POINT({lng} {lat})'),
                    radius
                )
            )

            if status := request.args.get('status'):
                base_query = base_query.filter(Issue.status == status)
            if category := request.args.get('category'):
                base_query = base_query.filter(Issue.category == category)

            issues = base_query.all()
            return jsonify([issue.to_dict() for issue in issues])

        except ValueError as ve:
            app.logger.error(f"Value error: {str(ve)}")
            return jsonify({'error': 'Invalid parameters'}), 400
        except Exception as e:
            app.logger.error(f"Unexpected error: {str(e)}")
            traceback.print_exc()
            return jsonify({'error': 'Internal server error'}), 500

    @app.route('/api/issues', methods=['POST'])
    @limiter.limit("10/minute")
    def create_issue():
        try:
            data = request.get_json() or {}
            required_fields = ['title', 'description', 'category', 'latitude', 'longitude']

            if not all(field in data for field in required_fields):
                return jsonify({'error': 'Missing required fields'}), 400

            # FIXED: Set default status if not provided
            status = data.get('status', 'reported')
            
            issue = Issue(
                title=data['title'],
                description=data['description'],
                category=data['category'],
                latitude=data['latitude'],
                longitude=data['longitude'],
                status=status,  # Use provided status or default to 'reported'
                user_id=data.get('user_id')
            )

            db.session.add(issue)
            db.session.commit()
            
            # Export to structured files
            exporter.export_issue(issue)
            
            socketio.emit('new_issue', issue.to_dict())

            return jsonify(issue.to_dict()), 201

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error creating issue: {str(e)}")
            return jsonify({'error': str(e)}), 500

    # Add new API endpoint to get exported data
    @app.route('/api/exported-issues', methods=['GET'])
    def get_exported_issues():
        """Get issues from exported structured data"""
        try:
            exported_data = exporter.get_exported_data()
            return jsonify(exported_data)
        except Exception as e:
            app.logger.error(f"Error reading exported data: {str(e)}")
            return jsonify({'error': 'Could not read exported data'}), 500

    # Initialize database and export existing data
    with app.app_context():
        try:
            if not inspect(db.engine).has_table('users'):
                print("Initializing database...")
                db.create_all()

                admin = User(
                    username='admin',
                    email='admin@publictrack.org',
                    is_admin=True
                )
                db.session.add(admin)
                db.session.commit()
                print("Database initialized successfully")
            
            # Ensure export file exists and export any existing issues
            exporter.ensure_export_file()
            exporter.export_all_issues()
            print("Data export system initialized")
            
        except Exception as e:
            print(f"Database initialization failed: {str(e)}")

    return app

def check_database_connection(app):
    """Comprehensive test of database connectivity and features"""
    with app.app_context():
        try:
            # 1. Basic connection test
            db.session.execute(text('SELECT 1')).scalar()
            app.logger.info("✅ Basic database connection successful")
            
            # 2. PostGIS availability test
            postgis_version = db.session.execute(text('SELECT PostGIS_version()')).scalar()
            app.logger.info(f"✅ PostGIS available (version: {postgis_version})")
            
            # 3. Tables existence check
            required_tables = {'users', 'issues'}
            inspector = inspect(db.engine)
            existing_tables = set(inspector.get_table_names())
            
            missing_tables = required_tables - existing_tables
            if missing_tables:
                app.logger.error(f"❌ Missing tables: {missing_tables}")
                return False
            app.logger.info("✅ All required tables exist")
            
            # 4. Spatial functions test
            try:
                test_point = 'POINT(77.5946 12.9716)'
                db.session.execute(
                    text("SELECT ST_DWithin(ST_GeogFromText(:point), ST_GeogFromText(:point), 1000)"),
                    {'point': test_point}
                ).scalar()
                app.logger.info("✅ Spatial functions working correctly")
            except Exception as e:
                app.logger.error(f"❌ Spatial functions test failed: {str(e)}")
                return False
            
            # 5. Trigger test
            try:
                test_issue = Issue(
                    title='TEST ISSUE - CAN BE DELETED',
                    description='Database connectivity test',
                    category='roads',
                    latitude=12.9716,
                    longitude=77.5946,
                    status='reported'  # Explicitly set status
                )
                db.session.add(test_issue)
                db.session.commit()
                
                created_issue = Issue.query.filter_by(title='TEST ISSUE - CAN BE DELETED').first()
                if not created_issue.location:
                    app.logger.error("❌ Trigger failed to set location")
                    return False
                
                app.logger.info("✅ Trigger working correctly")
                db.session.delete(created_issue)
                db.session.commit()
                
            except Exception as e:
                db.session.rollback()
                app.logger.error(f"❌ Trigger test failed: {str(e)}")
                return False
            
            return True
            
        except Exception as e:
            app.logger.error(f"❌ Database connection failed: {str(e)}")
            return False

def find_free_port(host='127.0.0.1'):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, 0))
    port = s.getsockname()[1]
    s.close()
    return port

if __name__ == '__main__':
    app = create_app()
    
    # Database connection check
    if check_database_connection(app):
        print("Database check passed successfully")
    else:
        print("Database check failed - see logs for details")
        sys.exit(1)

    # Server setup
    env_host = os.environ.get('HOST', '127.0.0.1')
    try:
        port = int(os.environ.get('PORT', find_free_port(env_host)))
    except Exception as e:
        print("Could not determine a free port:", e, file=sys.stderr)
        sys.exit(1)

    print(f"Starting PublicTrack Socket.IO server on {env_host}:{port}")
    try:
        socketio.run(
            app,
            host=env_host,
            port=port,
            debug=app.config.get('DEBUG', False),
            use_reloader=False
        )
    except OSError as oe:
        print(f"Failed to start server: {oe}", file=sys.stderr)
        sys.exit(1)