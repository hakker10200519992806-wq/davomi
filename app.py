from flask import Flask, render_template, request, jsonify, send_from_directory, session, Response
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit, join_room, leave_room
from datetime import datetime
import json, os, base64, zlib, hashlib

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(BASE_DIR, 'data')
STATIC    = os.path.join(BASE_DIR, 'static')

app = Flask(__name__,
            template_folder=os.path.join(BASE_DIR, 'templates'),
            static_folder=STATIC)
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DATA_DIR}/langlearn.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'langlearn-secret-' + hashlib.sha256(os.urandom(16)).hexdigest()[:16])

TEACHER_PASS_HASH = hashlib.sha256(b'200519992806').hexdigest()
UROK_MAGIC   = b'UROKFILE'
UROK_VERSION = 2

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _xor_bytes(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))

def encode_urok(payload: dict) -> str:
    raw        = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    compressed = zlib.compress(raw, level=9)
    key        = b'LangLearn2024xK9'
    xored      = _xor_bytes(compressed, key)
    final      = UROK_MAGIC + bytes([UROK_VERSION]) + xored
    return base64.b64encode(final).decode('ascii')

def decode_urok(b64_str: str) -> dict:
    try:
        raw = base64.b64decode(b64_str.strip())
    except Exception:
        raise ValueError("Base64 decode xatosi")
    if not raw.startswith(UROK_MAGIC):
        raise ValueError("Noto'g'ri fayl formati")
    data       = raw[len(UROK_MAGIC) + 1:]
    key        = b'LangLearn2024xK9'
    compressed = _xor_bytes(data, key)
    return json.loads(zlib.decompress(compressed).decode('utf-8'))

# ─── DB & SocketIO ────────────────────────────────────────────────────────────

db       = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

# ─── Association table ────────────────────────────────────────────────────────

group_members = db.Table('group_members',
    db.Column('group_id', db.Integer, db.ForeignKey('group.id'), primary_key=True),
    db.Column('user_id',  db.Integer, db.ForeignKey('user.id'),  primary_key=True),
)

# ─── Models ───────────────────────────────────────────────────────────────────

class User(db.Model):
    __tablename__ = 'user'
    id           = db.Column(db.Integer, primary_key=True)
    username     = db.Column(db.String(80),  unique=True, nullable=False)
    display      = db.Column(db.String(120), default='')
    role         = db.Column(db.String(20),  default='student')   # teacher | student
    blocked      = db.Column(db.Boolean,     default=False)
    password_hash= db.Column(db.String(128), default='')   # SHA-256
    avatar       = db.Column(db.String(200), default='')   # emoji yoki URL
    last_seen    = db.Column(db.DateTime,    nullable=True)
    created_at   = db.Column(db.DateTime,    default=datetime.utcnow)
    sent_messages     = db.relationship('ChatMessage', foreign_keys='ChatMessage.sender_id',
                                        backref='sender',   lazy=True)
    received_messages = db.relationship('ChatMessage', foreign_keys='ChatMessage.receiver_id',
                                        backref='receiver', lazy=True)
    owned_groups      = db.relationship('Group', backref='owner', lazy=True)
    def to_dict(self):
        return {'id': self.id, 'username': self.username,
                'display': self.display or self.username,
                'role': self.role, 'blocked': self.blocked,
                'avatar': self.avatar or '👤',
                'last_seen': self.last_seen.strftime('%d.%m.%Y %H:%M') if self.last_seen else None}
    def check_password(self, pwd):
        return self.password_hash == hashlib.sha256(pwd.encode()).hexdigest()
    def set_password(self, pwd):
        self.password_hash = hashlib.sha256(pwd.encode()).hexdigest()


class Group(db.Model):
    __tablename__ = 'group'
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(120), nullable=False)
    owner_id   = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    members    = db.relationship('User', secondary=group_members,
                                 backref=db.backref('groups', lazy=True), lazy=True)
    messages   = db.relationship('ChatMessage', foreign_keys='ChatMessage.group_id',
                                 backref='group', lazy=True, cascade='all, delete-orphan')
    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'owner_id': self.owner_id,
                'member_count': len(self.members),
                'members': [m.to_dict() for m in self.members]}


class ChatMessage(db.Model):
    __tablename__ = 'chat_message'
    id          = db.Column(db.Integer, primary_key=True)
    sender_id   = db.Column(db.Integer, db.ForeignKey('user.id'),  nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey('user.id'),  nullable=True)
    group_id    = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True)
    scope       = db.Column(db.String(20), default='global')   # global | private | group
    content     = db.Column(db.Text, nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    def to_dict(self):
        return {'id': self.id, 'sender_id': self.sender_id,
                'sender_name': self.sender.display or self.sender.username,
                'sender_role': self.sender.role,
                'receiver_id': self.receiver_id, 'group_id': self.group_id,
                'scope': self.scope, 'content': self.content,
                'created_at': self.created_at.strftime('%H:%M %d.%m.%Y')}


class Announcement(db.Model):
    __tablename__ = 'announcement'
    id         = db.Column(db.Integer, primary_key=True)
    author_id  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title      = db.Column(db.String(200), nullable=False)
    body       = db.Column(db.Text, default='')
    important  = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    author     = db.relationship('User', backref='announcements')
    def to_dict(self):
        return {'id': self.id, 'title': self.title, 'body': self.body,
                'important': self.important,
                'author': self.author.display or self.author.username,
                'created_at': self.created_at.strftime('%d.%m.%Y %H:%M')}


class Schedule(db.Model):
    __tablename__ = 'schedule'
    id         = db.Column(db.Integer, primary_key=True)
    title      = db.Column(db.String(200), nullable=False)
    event_type = db.Column(db.String(30),  default='lesson')  # lesson|assignment|test|meeting
    date       = db.Column(db.String(20),  nullable=False)
    time_start = db.Column(db.String(10),  default='')
    time_end   = db.Column(db.String(10),  default='')
    group_id   = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    def to_dict(self):
        return {'id': self.id, 'title': self.title, 'event_type': self.event_type,
                'date': self.date, 'time_start': self.time_start, 'time_end': self.time_end,
                'group_id': self.group_id, 'created_by': self.created_by}


class Resource(db.Model):
    __tablename__ = 'resource'
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(200), nullable=False)
    res_type    = db.Column(db.String(20),  default='link')  # pdf|video|audio|ebook|link
    url         = db.Column(db.Text, nullable=False)
    description = db.Column(db.Text, default='')
    group_id    = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    def to_dict(self):
        return {'id': self.id, 'title': self.title, 'res_type': self.res_type,
                'url': self.url, 'description': self.description,
                'group_id': self.group_id, 'uploaded_by': self.uploaded_by,
                'created_at': self.created_at.strftime('%d.%m.%Y')}


class Attendance(db.Model):
    __tablename__ = 'attendance'
    id        = db.Column(db.Integer, primary_key=True)
    user_id   = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date      = db.Column(db.String(20), nullable=False)
    status    = db.Column(db.String(20), default='absent')  # present|absent|online|late
    note      = db.Column(db.String(200), default='')
    group_id  = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True)
    marked_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    user      = db.relationship('User', foreign_keys=[user_id], backref='attendances')
    def to_dict(self):
        return {'id': self.id, 'user_id': self.user_id,
                'username': self.user.display or self.user.username,
                'date': self.date, 'status': self.status,
                'note': self.note, 'group_id': self.group_id}


class VideoLesson(db.Model):
    __tablename__ = 'video_lesson'
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(200), nullable=False)
    url         = db.Column(db.Text, nullable=False)
    thumbnail   = db.Column(db.Text, default='')
    duration    = db.Column(db.Integer, default=0)
    group_id    = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    def to_dict(self):
        return {'id': self.id, 'title': self.title, 'url': self.url,
                'thumbnail': self.thumbnail, 'duration': self.duration,
                'group_id': self.group_id, 'uploaded_by': self.uploaded_by,
                'created_at': self.created_at.strftime('%d.%m.%Y')}


class VideoProgress(db.Model):
    __tablename__ = 'video_progress'
    id        = db.Column(db.Integer, primary_key=True)
    video_id  = db.Column(db.Integer, db.ForeignKey('video_lesson.id'), nullable=False)
    user_id   = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    position  = db.Column(db.Float, default=0)
    completed = db.Column(db.Boolean, default=False)


class Lesson(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    title      = db.Column(db.String(200), nullable=False)
    subtitle   = db.Column(db.String(200), default='')
    order      = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    blocks     = db.relationship('Block', backref='lesson', lazy=True,
                                 cascade='all, delete-orphan', order_by='Block.order')


class Block(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    lesson_id  = db.Column(db.Integer, db.ForeignKey('lesson.id'), nullable=False)
    type       = db.Column(db.String(50), nullable=False)
    order      = db.Column(db.Integer, default=0)
    data       = db.Column(db.Text, default='{}')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    def to_dict(self):
        return {'id': self.id, 'lesson_id': self.lesson_id, 'type': self.type,
                'order': self.order, 'data': json.loads(self.data or '{}')}


class StudentProgress(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    block_id   = db.Column(db.Integer, db.ForeignKey('block.id'), nullable=False)
    answers    = db.Column(db.Text, default='{}')
    score      = db.Column(db.Float, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StudentResult(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    student_name = db.Column(db.String(200), default="O'quvchi")
    lesson_title = db.Column(db.String(200), default='')
    total_score  = db.Column(db.Float, default=0)
    max_score    = db.Column(db.Float, default=0)
    answers_json = db.Column(db.Text, default='{}')
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)

# ─── Init DB ──────────────────────────────────────────────────────────────────

def _seed_platform_users():
    t  = User(username='teacher1', display='Ustoz Alisher',    role='teacher')
    t.set_password('200519992806')
    s1 = User(username='student1', display='Jasur Abdullayev', role='student')
    s1.set_password('student1')
    s2 = User(username='student2', display='Malika Karimova',  role='student')
    s2.set_password('student2')
    s3 = User(username='student3', display='Sardor Toshmatov', role='student')
    s3.set_password('student3')
    # Default avatarlar
    t.avatar  = '👨‍🏫'
    s1.avatar = '👨‍🎓'
    s2.avatar = '👩‍🎓'
    s3.avatar = '🎓'
    db.session.add_all([t, s1, s2, s3])
    db.session.flush()
    grp = Group(name='A1 Guruh', owner_id=t.id, members=[s1, s2, s3])
    db.session.add(grp)
    ann = Announcement(author_id=t.id, title='Xush kelibsiz! 👋',
                       body='LangLearn platformasiga xush kelibsiz!', important=True)
    db.session.add(ann)
    db.session.commit()


def init_db():
    for d in [DATA_DIR, os.path.join(STATIC,'audio'), os.path.join(STATIC,'img'),
              os.path.join(STATIC,'video'), os.path.join(STATIC,'resources'),
              os.path.join(STATIC,'avatars'), os.path.join(STATIC,'homework'),
              os.path.join(BASE_DIR,'templates')]:
        os.makedirs(d, exist_ok=True)
    with app.app_context():
        db.create_all()
        uc = db.session.execute(db.select(db.func.count()).select_from(User)).scalar()
        if uc == 0:
            _seed_platform_users()
        lc = db.session.execute(db.select(db.func.count()).select_from(Lesson)).scalar()
        if lc == 0:
            seed_demo()


def seed_demo():
    # Dars 1: СТУДЕНТ! (Greetings & Introduction - Red Kalinka A1)
    lesson1 = Lesson(title='Дарс 1: СТУДЕНТ!', subtitle='A1', order=1)
    db.session.add(lesson1)
    db.session.flush()

    blocks_data_1 = [
        (0, 'heading', {'text': 'СТУДЕНТ! Salomlashish va Tanishish', 'level': 1, 'color': '#e63946', 'bg': ''}),
        (1, 'hr', {'color': '#e63946'}),
        
        # Новые слова
        (2, 'vocab', {
            'title': 'Янги сўзлар (Новые слова)', 'bar_color': '#457b9d',
            'items': [
                {'ru': 'Привет!', 'uz': 'Salom!', 'audio': ''},
                {'ru': 'Доброе утро!', 'uz': 'Xayrli tong!', 'audio': ''},
                {'ru': 'Добрый день!', 'uz': 'Xayrli kun!', 'audio': ''},
                {'ru': 'Добрый вечер!', 'uz': 'Xayrli kech!', 'audio': ''},
                {'ru': 'тоже', 'uz': 'ham', 'audio': ''},
                {'ru': 'очень', 'uz': 'juda', 'audio': ''},
                {'ru': 'Как дела?', 'uz': 'Qalaysan?', 'audio': ''},
                {'ru': 'Как у тебя дела?', 'uz': 'Sening qalaying?', 'audio': ''},
                {'ru': 'Как у вас дела?', 'uz': 'Sizning qalaying?', 'audio': ''},
                {'ru': 'А у тебя?', 'uz': 'Seningcha?', 'audio': ''},
                {'ru': 'А у вас?', 'uz': 'Sizingcha?', 'audio': ''},
                {'ru': 'отлично', 'uz': 'juda yaxshi', 'audio': ''},
                {'ru': 'хорошо - плохо', 'uz': 'yaxshi - yomon', 'audio': ''},
                {'ru': 'нормально', 'uz': 'oddiy', 'audio': ''},
                {'ru': 'пока', 'uz': 'xayr', 'audio': ''},
                {'ru': 'студент', 'uz': 'talaba', 'audio': ''},
                {'ru': 'студентка', 'uz': 'talaba (ayol)', 'audio': ''},
                {'ru': 'спасибо', 'uz': 'rahmat', 'audio': ''},
                {'ru': 'Как вас зовут?', 'uz': 'Ismingiz nima?', 'audio': ''},
                {'ru': 'Меня зовут...', 'uz': 'Mening ismim...', 'audio': ''},
                {'ru': 'я', 'uz': 'men', 'audio': ''},
                {'ru': 'ты', 'uz': 'sen', 'audio': ''},
                {'ru': 'он', 'uz': 'u', 'audio': ''},
                {'ru': 'она', 'uz': 'u (ayol)', 'audio': ''},
                {'ru': 'оно', 'uz': 'u (jansiz)', 'audio': ''},
                {'ru': 'мы', 'uz': 'biz', 'audio': ''},
                {'ru': 'вы', 'uz': 'siz', 'audio': ''},
                {'ru': 'они', 'uz': 'ular', 'audio': ''},
            ]
        }),
        
        # Диалог 1
        (3, 'dialog', {
            'title': 'Диалог 1 (Один - Один): Salomlashish',
            'lines': [
                {'speaker': 'A', 'text': 'Привет, Антон!'},
                {'speaker': 'B', 'text': 'Привёт, Наташа!'},
                {'speaker': 'A', 'text': 'Как дела?'},
                {'speaker': 'B', 'text': 'Спасибо, хорошо. А у тебя?'},
                {'speaker': 'A', 'text': 'Тоже хорошо.'},
                {'speaker': 'B', 'text': 'Пока.'},
                {'speaker': 'A', 'text': 'Пока.'},
            ]
        }),
        
        # Диалог 2
        (4, 'dialog', {
            'title': 'Диалог 2 (Два): Rasmiy salomlashish',
            'lines': [
                {'speaker': 'A', 'text': 'Привёт, Катя!'},
                {'speaker': 'B', 'text': 'Доброе утро, Саша!'},
                {'speaker': 'A', 'text': 'Как дела?'},
                {'speaker': 'B', 'text': 'Нормально. А у тебя?'},
                {'speaker': 'A', 'text': 'Тоже нормально.'},
                {'speaker': 'B', 'text': 'Пока!'},
                {'speaker': 'A', 'text': 'Пока!'},
            ]
        }),
        
        # Грамматика: Таблица местоимений
        (5, 'table', {
            'title': 'Грамматика: Местоимения (Hamma kelishdagi)',
            'headers': ['Именительный падеж', 'Винительный падеж (Объект)', 'Перевод на Ўзбекча'],
            'rows': [
                ['я', 'меня', 'men'],
                ['ты', 'тебя', 'sen'],
                ['он', 'его', 'u (erkak)'],
                ['она', 'её', 'u (ayol)'],
                ['оно', 'его', 'u (jansiz)'],
                ['мы', 'нас', 'biz'],
                ['вы', 'вас', 'siz'],
                ['они', 'их', 'ular'],
            ]
        }),
        
        # Упражнение 1: Заполнение пропусков
        (6, 'fill_blank', {
            'title': 'Машқ 1: Сўзни то\'лдириб чиқинг',
            'bar_color': '#2a9d8f',
            'instruction': 'Тўғри жавобни танланг:',
            'items': [
                {'pre': '- Привет!', 'answer': 'Привет', 'post': '!'},
                {'pre': '- Как дела?', 'answer': 'Хорошо', 'post': '.'},
                {'pre': '- Спасибо,', 'answer': 'спасибо', 'post': '. А у тебя?'},
                {'pre': '- Я студент,', 'answer': 'студентка', 'post': '.'},
            ]
        }),
        
        # Quiz
        (7, 'quiz', {
            'title': 'Мини-тест: Қайси варианти тўғри?',
            'bar_color': '#6a4c93',
            'questions': [
                {
                    'q': '1. "Привет" нинг маъноси қай варианты тўғри?',
                    'options': ['Xayrli kech!', 'Salom!', 'Rahmat', 'Xayr'],
                    'correct': 1
                },
                {
                    'q': '2. Рас шахсий сўзнинг номи (личное местоимение) қайси?',
                    'options': ['они', 'очень', 'студент', 'спасибо'],
                    'correct': 0
                },
                {
                    'q': '3. "Добрый день!" қай вақтда айтилади?',
                    'options': ['Тонг вақтида', 'Kun o\'rtasida', 'Кеч вақтида', 'Туни'],
                    'correct': 1
                },
                {
                    'q': '4. "Как у вас дела?" - бу қайси шакли сўз?',
                    'options': ['Раsmiy', 'Notаsmiy', 'Qimosiy', 'Ziyoiy'],
                    'correct': 0
                },
                {
                    'q': '5. "они" сўзининг маъноси?',
                    'options': ['u (erkak)', 'biz', 'ular', 'siz'],
                    'correct': 2
                },
            ]
        }),
        
        # Vocab timer
        (8, 'vocab_timer', {
            'title': 'Луғат вақти: Сўзларни ёдлаб олинг (120 сония)',
            'timer_sec': 120,
            'test_order': 'random',
            'test_dir': 'random',
            'items': [
                {'ru': 'Привет!', 'uz': 'Salom!'},
                {'ru': 'Доброе утро!', 'uz': 'Xayrli tong!'},
                {'ru': 'Добрый день!', 'uz': 'Xayrli kun!'},
                {'ru': 'Добрый вечер!', 'uz': 'Xayrli kech!'},
                {'ru': 'Как дела?', 'uz': 'Qalaysan?'},
                {'ru': 'спасибо', 'uz': 'rahmat'},
                {'ru': 'пока', 'uz': 'xayr'},
                {'ru': 'студент', 'uz': 'talaba'},
            ]
        }),
    ]
    
    for order, btype, bdata in blocks_data_1:
        b = Block(lesson_id=lesson1.id, type=btype, order=order,
                  data=json.dumps(bdata, ensure_ascii=False))
        db.session.add(b)
    
    db.session.commit()
    
    # Dars 2: ЭТО МОЙ ДРУГ (Family & Friends - Red Kalinka A1)
    lesson2 = Lesson(title='Дарс 2: ЭТО МОЙ ДРУГ', subtitle='A1', order=2)
    db.session.add(lesson2)
    db.session.flush()

    blocks_data_2 = [
        (0, 'heading', {'text': 'ЭТО МОЙ ДРУГ - Oila va Do\'stlar', 'level': 1, 'color': '#e63946', 'bg': ''}),
        (1, 'hr', {'color': '#e63946'}),
        
        # Новые слова
        (2, 'vocab', {
            'title': 'Янги сўзлар (Новые слова)', 'bar_color': '#457b9d',
            'items': [
                {'ru': 'Здравствуйте!', 'uz': 'Assalamu alaikum! (rasmiy)', 'audio': ''},
                {'ru': 'Как ваша фамилия?', 'uz': 'Sizning familiyangiz nima?', 'audio': ''},
                {'ru': 'Моя фамилия ...', 'uz': 'Mening familiyam...', 'audio': ''},
                {'ru': 'Как ваше отчество?', 'uz': 'Sizning otchestvoingiz nima?', 'audio': ''},
                {'ru': 'Моё отчество ...', 'uz': 'Mening otchestvom...', 'audio': ''},
                {'ru': 'Как ваше имя?', 'uz': 'Sizning ismingiz nima?', 'audio': ''},
                {'ru': 'Моё имя ...', 'uz': 'Mening ismim...', 'audio': ''},
                {'ru': 'очень приятно', 'uz': 'juda xursand', 'audio': ''},
                {'ru': 'мне тоже', 'uz': 'men ham', 'audio': ''},
                {'ru': 'друг', 'uz': 'do\'st', 'audio': ''},
                {'ru': 'подруга', 'uz': 'do\'st (ayol)', 'audio': ''},
                {'ru': 'коллега', 'uz': 'hamkasb', 'audio': ''},
                {'ru': 'преподаватель', 'uz': 'o\'qituvchi', 'audio': ''},
                {'ru': 'упражнение', 'uz': 'mashq', 'audio': ''},
                {'ru': 'книга', 'uz': 'kitob', 'audio': ''},
                {'ru': 'Что это?', 'uz': 'Bu nima?', 'audio': ''},
                {'ru': 'Кто это?', 'uz': 'Bu kim?', 'audio': ''},
                {'ru': 'Это мой друг', 'uz': 'Bu mening do\'stim', 'audio': ''},
                {'ru': 'Это моя подруга', 'uz': 'Bu mening do\'stim (ayol)', 'audio': ''},
                {'ru': 'Это мой коллега', 'uz': 'Bu mening hamkasbim', 'audio': ''},
                {'ru': 'до свидания', 'uz': 'xayr', 'audio': ''},
                {'ru': 'до завтра', 'uz': 'ertangi kun ko\'rinishmiz', 'audio': ''},
                {'ru': 'пожалуйста', 'uz': 'iltimos', 'audio': ''},
                {'ru': 'извини', 'uz': 'kechirasiz', 'audio': ''},
                {'ru': 'извините', 'uz': 'kechirasiz (rasmiy)', 'audio': ''},
                {'ru': 'ничего', 'uz': 'hechnarsa emas', 'audio': ''},
                {'ru': 'можно?', 'uz': 'oladimi?', 'audio': ''},
                {'ru': 'вопрос', 'uz': 'savol', 'audio': ''},
            ]
        }),
        
        # Диалог 1
        (3, 'dialog', {
            'title': 'Диалог 1 (Один): Do\'st bilan tanishish',
            'lines': [
                {'speaker': 'A', 'text': 'Здравствуйте! Меня зовут Наташа. А вас?'},
                {'speaker': 'B', 'text': 'А меня зовут Иван.'},
                {'speaker': 'A', 'text': 'Очень приятно.'},
                {'speaker': 'B', 'text': 'Мне тоже.'},
                {'speaker': 'A', 'text': 'До свидания.'},
                {'speaker': 'B', 'text': 'До завтра.'},
            ]
        }),
        
        # Диалог 2
        (4, 'dialog', {
            'title': 'Диалог 2 (Два): Oila tanitish',
            'lines': [
                {'speaker': 'A', 'text': 'Добрый день. Меня зовут Антон Иванович Макаров. А вас?'},
                {'speaker': 'B', 'text': 'Меня зовут Елена Борисовна Иванова.'},
                {'speaker': 'A', 'text': 'Очень приятно.'},
                {'speaker': 'B', 'text': 'Мне тоже. До свидания.'},
                {'speaker': 'A', 'text': 'До свидания.'},
            ]
        }),
        
        # Грамматика: Таблица 1 - Род существительных (он, она, оно)
        (5, 'table', {
            'title': 'Грамматика Таблица 1: Род существительных (он, она, оно)',
            'headers': ['Erkak (он)', 'Ayol (она)', 'Jansiz (оно)'],
            'rows': [
                ['друг', 'подруга', 'утро'],
                ['студент', 'студентка', 'отчество'],
                ['чай', 'Наташа', 'упражнение'],
                ['день', 'фамилия', 'море'],
                ['преподаватель', 'мать', 'имя'],
                ['согласная б', 'а / я в', 'о / е мя'],
            ]
        }),
        
        # Грамматика: Таблица 2 - Притяжательные местоимения
        (6, 'table', {
            'title': 'Грамматика Таблица 2: Притяжательные местоимения (Birgalik sozneri)',
            'headers': ['М. (erkak)', 'Ж. (ayol)', 'Ср. (jansiz)', 'Мн. ч. (ko\'plik)'],
            'rows': [
                ['мой', 'моя', 'моё', 'мои'],
                ['твой', 'твоя', 'твоё', 'твои'],
                ['его', 'его', 'его', 'его'],
                ['её', 'её', 'её', 'её'],
                ['наш', 'наша', 'наше', 'наши'],
                ['ваш', 'ваша', 'ваше', 'ваши'],
                ['их', 'их', 'их', 'их'],
            ]
        }),
        
        # Упражнение 1: Заполнение пропусков
        (7, 'fill_blank', {
            'title': 'Машқ 1: Мой, моя, моё ёки твой, твоя, твоё танланг',
            'bar_color': '#2a9d8f',
            'instruction': 'Тўғри жавобни танланг:',
            'items': [
                {'pre': 'Это', 'answer': 'мой', 'post': 'друг. (erkak)'},
                {'pre': 'Это', 'answer': 'моя', 'post': 'подруга. (ayol)'},
                {'pre': 'Это', 'answer': 'мой', 'post': 'коллега. (erkak)'},
                {'pre': 'Где', 'answer': 'твоя', 'post': 'книга? (ayol)'},
                {'pre': 'Где', 'answer': 'мой', 'post': 'преподаватель? (erkak)'},
            ]
        }),
        
        # Упражнение 2: Определите род слов
        (8, 'fill_blank', {
            'title': 'Машқ 2: Rod soznalarini aniqlang (он, она, оно)',
            'bar_color': '#2a9d8f',
            'instruction': 'Rod katugoriyasini toping:',
            'items': [
                {'pre': 'подруга -', 'answer': 'она', 'post': ''},
                {'pre': 'студент -', 'answer': 'он', 'post': ''},
                {'pre': 'книга -', 'answer': 'она', 'post': ''},
                {'pre': 'имя -', 'answer': 'оно', 'post': ''},
                {'pre': 'день -', 'answer': 'он', 'post': ''},
                {'pre': 'вечер -', 'answer': 'он', 'post': ''},
                {'pre': 'утро -', 'answer': 'оно', 'post': ''},
            ]
        }),
        
        # Упражнение 3: Переформулируйте предложения
        (9, 'fill_blank', {
            'title': 'Машқ 3: Gaplarni o\'zgartiring (Мной имя / Меня зовут)',
            'bar_color': '#2a9d8f',
            'instruction': 'Namuna: Мое имя - Наташа. → Меня зовут Наташа.',
            'items': [
                {'pre': 'Твое имя - Лаура. →', 'answer': 'Тебя зовут Лаура', 'post': ''},
                {'pre': 'Его имя - Стивен. →', 'answer': 'Его зовут Стивен', 'post': ''},
                {'pre': 'Ваше имя - Иван. →', 'answer': 'Вас зовут Иван', 'post': ''},
                {'pre': 'Её имя - Таня. →', 'answer': 'Её зовут Таня', 'post': ''},
            ]
        }),
        
        # Упражнение 5: Заполните пропуски в тексте
        (10, 'fill_blank', {
            'title': 'Машқ 5: Matnda prorskini to\'ldiring',
            'bar_color': '#2a9d8f',
            'instruction': 'Birgalik sozlarini tanlanib matnni to\'ldiring:',
            'items': [
                {'pre': 'Меня зовут Антон.', 'answer': 'Моя', 'post': 'фамилия - Иванов. Моё отчество - Николаевич.'},
                {'pre': 'Это', 'answer': 'мой', 'post': 'друг. Его зовут Джон. Его фамилия - Паркер. Это подруга. Её зовут Наташа. Её фамилия - Романова.'},
                {'pre': 'А как', 'answer': 'вас', 'post': 'зовут? Как ваша фамилия? Как ваше отчество?'},
            ]
        }),
        
        # Упражнение 6: Вставьте притяжательные местоимения
        (11, 'fill_blank', {
            'title': 'Машқ 6: Birgalik sozlarini to\'g\'ri joyga qo\'ying',
            'bar_color': '#2a9d8f',
            'instruction': 'мой, твой, наш, ваш so\'zlarini tanlanib to\'ldiring:',
            'items': [
                {'pre': 'мой:', 'answer': 'мой', 'post': 'друг / моя подруга / моё имя'},
                {'pre': 'твой:', 'answer': 'твой', 'post': 'студент / твоя фамилия / твоё отчество'},
                {'pre': 'наш:', 'answer': 'наш', 'post': 'преподаватель / наша студентка / наше утро'},
                {'pre': 'ваш:', 'answer': 'ваш', 'post': 'вопрос / ваша книга / ваше упражнение'},
            ]
        }),
        
        # Упражнение 7: Заполните пропуски
        (12, 'fill_blank', {
            'title': 'Машқ 7: Prorskini to\'ldiring',
            'bar_color': '#2a9d8f',
            'instruction': 'Birgalik sozlarini bilgan holda to\'ldiring:',
            'items': [
                {'pre': '- Кто это? Это', 'answer': 'моя', 'post': 'подруга. Её зовут Лена.'},
                {'pre': '- Кто это? Это', 'answer': 'наш', 'post': 'преподаватель. Его зовут Николай Петрович.'},
                {'pre': '- Кто это? Это', 'answer': 'его', 'post': 'коллега. Её зовут Ольга Романова.'},
                {'pre': '- Кто это? Это', 'answer': 'мой', 'post': 'студент. Его зовут Стив.'},
                {'pre': '- Кто это? Это', 'answer': 'ваша', 'post': 'мать. Её зовут Татьяна.'},
                {'pre': '- Кто это? Это', 'answer': 'твоя', 'post': 'студентка. Её зовут Марта.'},
                {'pre': '- Кто это? Это я.', 'answer': 'Мое', 'post': 'имя - Виктор.'},
            ]
        }),
        
        # Упражнение 8: Обсуждение по примеру
        (13, 'fill_blank', {
            'title': 'Машқ 8: Namuna bo\'yicha dialog tuzilng',
            'bar_color': '#2a9d8f',
            'instruction': 'Namuna: Это мой подруга Синтия. → Её зовут Синтия. Её имя - Синтия.',
            'items': [
                {'pre': '- Это я, Паблю. →', 'answer': 'Его зовут Паблю', 'post': ''},
                {'pre': '- Это мой друг Джон. →', 'answer': 'Его зовут Джон', 'post': ''},
                {'pre': '- Это ты, Саманта. →', 'answer': 'Её зовут Саманта', 'post': ''},
                {'pre': '- Это вы, Иван Иванович? →', 'answer': 'Вас зовут Иван Иванович', 'post': ''},
                {'pre': '- Это мой коллега Стив. →', 'answer': 'Его зовут Стив', 'post': ''},
            ]
        }),
        
        # О себе - Open-ended questions
        (14, 'fill_blank', {
            'title': 'О себе: O\'zingiz haqida javob bering',
            'bar_color': '#e76f51',
            'instruction': 'Quyidagi savollarga javob bering:',
            'items': [
                {'pre': '1. Как тебе зовут?', 'answer': '[Sizning ismingiz]', 'post': ''},
                {'pre': '2. Как твой фамилия?', 'answer': '[Sizning familiyangiz]', 'post': ''},
                {'pre': '3. Как твое отчество?', 'answer': '[Sizning otchestvoingiz]', 'post': ''},
                {'pre': '4. Ты преподаватель? Ты студент?', 'answer': '[Ha yoki Yo\'q]', 'post': ''},
                {'pre': '5. А твой преподаватель? Как его зовут? Как его фамилия?', 'answer': '[O\'qituvchingizning ismi]', 'post': ''},
                {'pre': '6. А твой друг? Как его зовут? Как его фамилия?', 'answer': '[Do\'stingizning ismi]', 'post': ''},
                {'pre': '7. А твоя подруга? Как её зовут? Как её фамилия?', 'answer': '[Do\'stingizning ismi (ayol)]', 'post': ''},
            ]
        }),
        
        # Упражнение 5: Quiz
        (15, 'quiz', {
            'title': 'Мини-тест: "Это мой друг" mavzusi - Grammatika',
            'bar_color': '#6a4c93',
            'questions': [
                {
                    'q': '1. "Что это?" нинг маъноси?',
                    'options': ['Bu kim?', 'Bu nima?', 'Qaysi?', 'Qachon?'],
                    'correct': 1
                },
                {
                    'q': '2. "друг" сўзининг rodi?',
                    'options': ['она (ayol)', 'он (erkak)', 'оно (jansiz)', 'они (ko\'p)'],
                    'correct': 1
                },
                {
                    'q': '3. "подруга" сўзининг rodi?',
                    'options': ['она', 'он', 'оно', 'они'],
                    'correct': 0
                },
                {
                    'q': '4. "Его имя - Антон" қай шакли to\'g\'ri?',
                    'options': ['Его зовут Антон', 'Ему зовут Антон', 'Его зовут Антона', 'Его зовут Антона'],
                    'correct': 0
                },
                {
                    'q': '5. Притяжательный местоимение (birgalik sozlari) қайси?',
                    'options': ['я, ты, он', 'мой, твой, его', 'какой, какая, какое', 'что, кто, где'],
                    'correct': 1
                },
            ]
        }),
        
        # Vocab timer - final - yangilangan
        (16, 'vocab_timer', {
            'title': 'Yakuniy Luғat vaqti: Dars 2 barcha soznlarini yodlab oling (200 s)',
            'timer_sec': 200,
            'test_order': 'random',
            'test_dir': 'random',
            'items': [
                {'ru': 'мой', 'uz': 'mening (erkak)'},
                {'ru': 'моя', 'uz': 'mening (ayol)'},
                {'ru': 'моё', 'uz': 'mening (jansiz)'},
                {'ru': 'мои', 'uz': 'mening (ko\'p)'},
                {'ru': 'твой', 'uz': 'sening (erkak)'},
                {'ru': 'твоя', 'uz': 'sening (ayol)'},
                {'ru': 'твоё', 'uz': 'sening (jansiz)'},
                {'ru': 'твои', 'uz': 'sening (ko\'p)'},
                {'ru': 'его', 'uz': 'uning (erkak)'},
                {'ru': 'её', 'uz': 'uning (ayol)'},
                {'ru': 'наш', 'uz': 'bizning (erkak)'},
                {'ru': 'наша', 'uz': 'bizning (ayol)'},
                {'ru': 'наше', 'uz': 'bizning (jansiz)'},
                {'ru': 'наши', 'uz': 'bizning (ko\'p)'},
                {'ru': 'ваш', 'uz': 'sizning (erkak)'},
                {'ru': 'ваша', 'uz': 'sizning (ayol)'},
                {'ru': 'ваше', 'uz': 'sizning (jansiz)'},
                {'ru': 'ваши', 'uz': 'sizning (ko\'p)'},
                {'ru': 'их', 'uz': 'ularning'},
                {'ru': 'друг', 'uz': 'do\'st'},
                {'ru': 'подруга', 'uz': 'do\'st (ayol)'},
                {'ru': 'коллега', 'uz': 'hamkasb'},
                {'ru': 'преподаватель', 'uz': 'o\'qituvchi'},
                {'ru': 'мать', 'uz': 'ona'},
                {'ru': 'отчество', 'uz': 'otchestvo'},
                {'ru': 'фамилия', 'uz': 'familiya'},
                {'ru': 'имя', 'uz': 'ism'},
            ]
        }),
    ]
    
    for order, btype, bdata in blocks_data_2:
        b = Block(lesson_id=lesson2.id, type=btype, order=order,
                  data=json.dumps(bdata, ensure_ascii=False))
        db.session.add(b)
    
    db.session.commit()
    
    # Дарс 3: МОЯ СЕМЬЯ (Family - Red Kalinka A1) - COMPLETE VERSION
    lesson3 = Lesson(title='Дарс 3: МОЯ СЕМЬЯ', subtitle='A1', order=3)
    db.session.add(lesson3)
    db.session.flush()

    blocks_data_3 = [
        (0, 'heading', {'text': 'МОЯ СЕМЬЯ - Oila', 'level': 1, 'color': '#e63946', 'bg': ''}),
        (1, 'hr', {'color': '#e63946'}),
        
        # Новые слова (1-qism)
        (2, 'vocab', {
            'title': 'Янги сўзлар (Новые слова)', 'bar_color': '#457b9d',
            'items': [
                {'ru': 'Давайте познакомимся!', 'uz': 'Tanishamiz!', 'audio': ''},
                {'ru': 'семья', 'uz': 'oila', 'audio': ''},
                {'ru': 'родители', 'uz': 'ota-ona', 'audio': ''},
                {'ru': 'отец (папа)', 'uz': 'ota (opa)', 'audio': ''},
                {'ru': 'мать (мама)', 'uz': 'ona (oyi)', 'audio': ''},
                {'ru': 'муж', 'uz': 'eri', 'audio': ''},
                {'ru': 'жена', 'uz': 'xotini', 'audio': ''},
                {'ru': 'дети', 'uz': 'bolalar', 'audio': ''},
                {'ru': 'сын', 'uz': 'o\'g\'ul', 'audio': ''},
                {'ru': 'дочь', 'uz': 'qiz', 'audio': ''},
                {'ru': 'брат', 'uz': 'aka', 'audio': ''},
                {'ru': 'сестра', 'uz': 'singil', 'audio': ''},
                {'ru': 'бабушка', 'uz': 'buvi', 'audio': ''},
                {'ru': 'дедушка', 'uz': 'buva', 'audio': ''},
                {'ru': 'внук', 'uz': 'nevara', 'audio': ''},
                {'ru': 'внучка', 'uz': 'nevara (qiz)', 'audio': ''},
                {'ru': 'племянник', 'uz': 'xojanining o\'g\'ul', 'audio': ''},
                {'ru': 'племянница', 'uz': 'xojanining qiz', 'audio': ''},
                {'ru': 'тётя', 'uz': 'xoja (ayol)', 'audio': ''},
                {'ru': 'дядя', 'uz': 'xoja', 'audio': ''},
                {'ru': 'собака', 'uz': 'it', 'audio': ''},
                {'ru': 'кошка', 'uz': 'mushuk', 'audio': ''},
                {'ru': 'где?', 'uz': 'qaerda?', 'audio': ''},
                {'ru': 'Вот ...', 'uz': 'Mana...', 'audio': ''},
            ]
        }),
        
        # Диалог 1 (1-qism)
        (3, 'dialog', {
            'title': 'Диалог 1 (Один): Oila tanishuvi',
            'lines': [
                {'speaker': 'A', 'text': '- Meňa зовут Наташа. Это моя семья.'},
                {'speaker': 'B', 'text': '- Наташа, кто это?'},
                {'speaker': 'A', 'text': '- Это моя мама. Её зовут Татьяна Михайловна.'},
                {'speaker': 'B', 'text': '- А это твой отец?'},
                {'speaker': 'A', 'text': '- Нет, это мой дедушка. Его зовут Михаил Иванович.'},
                {'speaker': 'B', 'text': '- А где твой отец?'},
                {'speaker': 'A', 'text': '- Вот он. Его зовут Антон.'},
            ]
        }),
        
        # Текст: Моя семья (2-qism boshlanish)
        (4, 'audio_text', {
            'title': 'Matn: Моя семья',
            'audio': '',
            'text': 'Давайте познакомимся! Меня зовут Мария. Моя фамилия - Иванова. Это моя семья.\n\nЭто мой отец. Его зовут Николай. А это моя мать. Её зовут Наталья. Мой папа и моя мама – это мои родители.\n\nА это мой брат. Его зовут Андрей. Это его жена. Её зовут Катя. Это их дети: сын Дима и дочь Наташа. Дима – мой племянник, а Наташа – моя племянница. Я – их тётя.\n\nА это моя бабушка и мой дедушка. Я – их внучка, а Андрей – их внук.\n\nЭто моя тётя Елена и её муж Михаил. Михаил – мой дядя, а я – его племянница.\n\nВот наш дом. Это наша собака. Его зовут Шарик. А где наша кошка? Вот она! Мурка!'
        }),
        
        # Вопросы к тексту (2-qism)
        (5, 'fill_blank', {
            'title': 'Savollarga javob bering (Matnni o\'qib)',
            'bar_color': '#e76f51',
            'instruction': 'Matndan foydalanaraki javoblarni to\'ldiring:',
            'items': [
                {'pre': '1. Мария, твой фамилия - Петрова? → Нет, моя фамилия -', 'answer': 'Иванова', 'post': ''},
                {'pre': '2. Мария, Наталья - твоя сестра? →', 'answer': 'Нет, Наталья - моя мать', 'post': ''},
                {'pre': '3. Мария, Дима - твой дядя? →', 'answer': 'Нет, Дима - мой племянник', 'post': ''},
                {'pre': '4. Мария, Андрей - твой отец? →', 'answer': 'Нет, Андрей - мой брат', 'post': ''},
                {'pre': '5. Мария, Шарик - твоя кошка? →', 'answer': 'Нет, Шарик - моя собака', 'post': ''},
                {'pre': '6. Мария, Елена - твоя племянница? →', 'answer': 'Нет, Елена - моя тётя', 'post': ''},
                {'pre': '7. Мария, Михаил - твой брат? →', 'answer': 'Нет, Михаил - мой дядя', 'post': ''},
                {'pre': '8. Мария, Мурка - твоя бабушка? →', 'answer': 'Нет, Мурка - моя кошка', 'post': ''},
            ]
        }),
        
        # Машқ 1: Определите род слов (2-qism)
        (6, 'fill_blank', {
            'title': 'Машқ 1: Rod soznlarini aniqlang',
            'bar_color': '#2a9d8f',
            'instruction': 'Soznlarning rodini (род) yozing:',
            'items': [
                {'pre': '1. мать -', 'answer': 'она', 'post': ''},
                {'pre': '2. дедушка -', 'answer': 'он', 'post': ''},
                {'pre': '3. бабушка -', 'answer': 'она', 'post': ''},
                {'pre': '4. внучка -', 'answer': 'она', 'post': ''},
                {'pre': '5. родители -', 'answer': 'они', 'post': ''},
                {'pre': '6. брат -', 'answer': 'он', 'post': ''},
                {'pre': '7. племянник -', 'answer': 'он', 'post': ''},
                {'pre': '8. семья -', 'answer': 'она', 'post': ''},
                {'pre': '9. сестра -', 'answer': 'она', 'post': ''},
                {'pre': '10. дом -', 'answer': 'он', 'post': ''},
                {'pre': '11. собака -', 'answer': 'она', 'post': ''},
                {'pre': '12. тётя -', 'answer': 'она', 'post': ''},
                {'pre': '13. внук -', 'answer': 'он', 'post': ''},
                {'pre': '14. племянница -', 'answer': 'она', 'post': ''},
                {'pre': '15. дети -', 'answer': 'они', 'post': ''},
                {'pre': '16. муж -', 'answer': 'он', 'post': ''},
                {'pre': '17. кошка -', 'answer': 'она', 'post': ''},
                {'pre': '18. дядя -', 'answer': 'он', 'post': ''},
            ]
        }),
        
        # Машқ 2: Заполните пропуски (2-qism)
        (7, 'fill_blank', {
            'title': 'Машқ 2: Prorskini to\'ldiring (Ima / Jego / Yeyo)',
            'bar_color': '#2a9d8f',
            'instruction': 'Namuna: Это мой отец Николай. → Его имя - Николай. Его зовут Николай.',
            'items': [
                {'pre': '1. Это мой бабушка Нина. →', 'answer': 'Её имя - Нина. Её зовут Нина', 'post': ''},
                {'pre': '2. Это мой сын Андрей. →', 'answer': 'Его имя - Андрей. Его зовут Андрей', 'post': ''},
                {'pre': '3. Это мой внучка Лена. →', 'answer': 'Её имя - Лена. Её зовут Лена', 'post': ''},
                {'pre': '4. Это мой внучка Лена. →', 'answer': 'Её имя - Лена. Её зовут Лена', 'post': ''},
                {'pre': '5. Это мой дедушка Гена. →', 'answer': 'Его имя - Гена. Его зовут Гена', 'post': ''},
                {'pre': '6. Это мой сестра Ольга. →', 'answer': 'Её имя - Ольга. Её зовут Ольга', 'post': ''},
                {'pre': '7. Это мой кошка Мурка. →', 'answer': 'Её имя - Мурка. Её зовут Мурка', 'post': ''},
            ]
        }),
        
        # Машқ 3: Закончите предложения (2-qism)
        (8, 'fill_blank', {
            'title': 'Машқ 3: Gaplarni davom ettirisng',
            'bar_color': '#2a9d8f',
            'instruction': 'Namuna: Это моя мама. Надежда – её мать. Надежда – моя бабушка. А я – её внучка.',
            'items': [
                {'pre': '1. Это моя жена. Наташа – её дочь. Наташа -', 'answer': 'моя дочь', 'post': ''},
                {'pre': '2. Это моя жена. Наташа – её дочь. Наташа -', 'answer': 'моя дочь', 'post': ''},
                {'pre': '3. Это мой отец. Николай – его брат. Николай -', 'answer': 'мой дядя', 'post': ''},
                {'pre': '4. Это мой внук Дима. Лена – его сестра. Лена -', 'answer': 'моя внучка', 'post': ''},
            ]
        }),
        
        # Машқ 4: Образуйте притяжательные местоимения (2-qism)
        (9, 'fill_blank', {
            'title': 'Машқ 4: Birgalik sozlarni yasang',
            'bar_color': '#2a9d8f',
            'instruction': '(он) собака → его собака | (ты) кошка → твоя кошка',
            'items': [
                {'pre': '1. (я) дом →', 'answer': 'мой дом', 'post': ''},
                {'pre': '2. (он) собака →', 'answer': 'его собака', 'post': ''},
                {'pre': '3. (я) дедушка →', 'answer': 'мой дедушка', 'post': ''},
                {'pre': '4. (ты) мать →', 'answer': 'твоя мать', 'post': ''},
                {'pre': '5. (они) внучка →', 'answer': 'их внучка', 'post': ''},
                {'pre': '6. (она) сын →', 'answer': 'её сын', 'post': ''},
                {'pre': '7. (мы) сестра →', 'answer': 'наша сестра', 'post': ''},
                {'pre': '8. (он) дочь →', 'answer': 'его дочь', 'post': ''},
                {'pre': '9. (ты) родители →', 'answer': 'твои родители', 'post': ''},
                {'pre': '10. (вы) дети →', 'answer': 'ваши дети', 'post': ''},
                {'pre': '11. (я) внук →', 'answer': 'мой внук', 'post': ''},
                {'pre': '12. (она) тётя →', 'answer': 'её тётя', 'post': ''},
                {'pre': '13. (мы) племянник →', 'answer': 'наш племянник', 'post': ''},
                {'pre': '14. (ты) кошка →', 'answer': 'твоя кошка', 'post': ''},
                {'pre': '15. (они) дядя →', 'answer': 'их дядя', 'post': ''},
                {'pre': '16. (она) муж →', 'answer': 'её муж', 'post': ''},
                {'pre': '17. (вы) жена →', 'answer': 'ваша жена', 'post': ''},
                {'pre': '18. (я) брат →', 'answer': 'мой брат', 'post': ''},
            ]
        }),
        
        # Quiz
        (10, 'quiz', {
            'title': 'Мини-тест: Oila mavzusi',
            'bar_color': '#6a4c93',
            'questions': [
                {
                    'q': '1. "семья" нинг маъноси?',
                    'options': ['do\'st', 'oila', 'maktab', 'xona'],
                    'correct': 1
                },
                {
                    'q': '2. "папа" қай shaxsi?',
                    'options': ['она', 'он', 'оно', 'они'],
                    'correct': 1
                },
                {
                    'q': '3. Если мама – её мать, то бабушка – это?',
                    'options': ['её сестра', 'её дочь', 'её мать', 'её сын'],
                    'correct': 2
                },
                {
                    'q': '4. "внучка" қай soni?',
                    'options': ['он', 'она', 'оно', 'они'],
                    'correct': 1
                },
                {
                    'q': '5. "дети" қай soni?',
                    'options': ['он', 'она', 'оно', 'они'],
                    'correct': 3
                },
            ]
        }),
        
        # Vocab timer
        (11, 'vocab_timer', {
            'title': 'Луғат вақти: Oila sozlarini yodlab oling (150 s)',
            'timer_sec': 150,
            'test_order': 'random',
            'test_dir': 'random',
            'items': [
                {'ru': 'семья', 'uz': 'oila'},
                {'ru': 'отец', 'uz': 'ota'},
                {'ru': 'мать', 'uz': 'ona'},
                {'ru': 'папа', 'uz': 'opa'},
                {'ru': 'мама', 'uz': 'oyi'},
                {'ru': 'брат', 'uz': 'aka'},
                {'ru': 'сестра', 'uz': 'singil'},
                {'ru': 'жена', 'uz': 'xotini'},
                {'ru': 'муж', 'uz': 'eri'},
                {'ru': 'сын', 'uz': 'o\'g\'ul'},
                {'ru': 'дочь', 'uz': 'qiz'},
                {'ru': 'дедушка', 'uz': 'buva'},
                {'ru': 'бабушка', 'uz': 'buvi'},
                {'ru': 'внук', 'uz': 'nevara'},
                {'ru': 'внучка', 'uz': 'nevara (qiz)'},
                {'ru': 'дядя', 'uz': 'xoja'},
                {'ru': 'тётя', 'uz': 'xoja (ayol)'},
                {'ru': 'дом', 'uz': 'uy'},
                {'ru': 'собака', 'uz': 'it'},
                {'ru': 'кошка', 'uz': 'mushuk'},
                {'ru': 'родители', 'uz': 'ota-ona'},
            ]
        }),
    ]
    
    for order, btype, bdata in blocks_data_3:
        b = Block(lesson_id=lesson3.id, type=btype, order=order,
                  data=json.dumps(bdata, ensure_ascii=False))
        db.session.add(b)
    
    db.session.commit()

# ─── API: Rol (session) ───────────────────────────────────────────────────────

@app.route('/api/role', methods=['GET'])
def get_role():
    return jsonify({'role': session.get('role', None)})

@app.route('/api/role/set', methods=['POST'])
def set_role():
    d = request.json or {}
    role = d.get('role', 'student')
    if role == 'teacher':
        pwd = d.get('password', '')
        if hashlib.sha256(pwd.encode()).hexdigest() != TEACHER_PASS_HASH:
            return jsonify({'ok': False, 'error': 'Parol noto\'g\'ri'}), 401
    session['role'] = role
    session.permanent = True
    return jsonify({'ok': True, 'role': role})

@app.route('/api/role/logout', methods=['POST'])
def logout():
    session.pop('role', None)
    return jsonify({'ok': True})

# ─── API: Lessons ─────────────────────────────────────────────────────────────

@app.route('/api/lessons', methods=['GET'])
def get_lessons():
    lessons = db.session.execute(db.select(Lesson).order_by(Lesson.order)).scalars().all()
    return jsonify([{
        'id': l.id, 'title': l.title, 'subtitle': l.subtitle,
        'order': l.order, 'block_count': len(l.blocks)
    } for l in lessons])

@app.route('/api/lessons', methods=['POST'])
def create_lesson():
    d = request.json
    max_order = db.session.query(db.func.max(Lesson.order)).scalar() or 0
    lesson = Lesson(title=d['title'], subtitle=d.get('subtitle', ''), order=max_order + 1)
    db.session.add(lesson)
    db.session.commit()
    return jsonify({'id': lesson.id, 'title': lesson.title})

@app.route('/api/lessons/<int:lid>', methods=['PUT'])
def update_lesson(lid):
    lesson = db.session.get(Lesson, lid)
    if not lesson: return jsonify({'error':'Not found'}), 404
    d = request.json
    if 'title' in d:    lesson.title    = d['title']
    if 'subtitle' in d: lesson.subtitle = d['subtitle']
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/lessons/<int:lid>', methods=['DELETE'])
def delete_lesson(lid):
    lesson = db.session.get(Lesson, lid)
    if not lesson: return jsonify({'error':'Not found'}), 404
    db.session.delete(lesson)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/lessons/<int:lid>/duplicate', methods=['POST'])
def duplicate_lesson(lid):
    orig = db.session.get(Lesson, lid)
    if not orig: return jsonify({'error':'Not found'}), 404
    max_order = db.session.query(db.func.max(Lesson.order)).scalar() or 0
    new_l = Lesson(title=orig.title + ' (копия)', subtitle=orig.subtitle, order=max_order + 1)
    db.session.add(new_l)
    db.session.flush()
    for b in orig.blocks:
        nb = Block(lesson_id=new_l.id, type=b.type, order=b.order, data=b.data)
        db.session.add(nb)
    db.session.commit()
    return jsonify({'id': new_l.id, 'title': new_l.title})

# ─── API: Blocks ──────────────────────────────────────────────────────────────

@app.route('/api/lessons/<int:lid>/blocks', methods=['GET'])
def get_blocks(lid):
    blocks = db.session.execute(db.select(Block).filter_by(lesson_id=lid).order_by(Block.order)).scalars().all()
    return jsonify([b.to_dict() for b in blocks])

@app.route('/api/blocks', methods=['POST'])
def create_block():
    d = request.json
    max_order = db.session.query(db.func.max(Block.order)) \
                    .filter(Block.lesson_id == d['lesson_id']).scalar() or 0
    block = Block(
        lesson_id=d['lesson_id'],
        type=d['type'],
        order=d.get('order', max_order + 1),
        data=json.dumps(d.get('data', {}), ensure_ascii=False)
    )
    db.session.add(block)
    db.session.commit()
    return jsonify(block.to_dict())

@app.route('/api/blocks/<int:bid>', methods=['PUT'])
def update_block(bid):
    block = db.session.get(Block, bid)
    if not block: return jsonify({'error':'Not found'}), 404
    d = request.json
    if 'data' in d:  block.data  = json.dumps(d['data'], ensure_ascii=False)
    if 'order' in d: block.order = d['order']
    db.session.commit()
    return jsonify(block.to_dict())

@app.route('/api/blocks/<int:bid>', methods=['DELETE'])
def delete_block(bid):
    block = db.session.get(Block, bid)
    if not block: return jsonify({'error':'Not found'}), 404
    db.session.delete(block)
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/blocks/reorder', methods=['POST'])
def reorder_blocks():
    for item in request.json:
        b = db.session.get(Block, item['id'])
        if b: b.order = item['order']
    db.session.commit()
    return jsonify({'ok': True})

# ─── API: Progress ────────────────────────────────────────────────────────────

@app.route('/api/progress/<int:bid>', methods=['GET'])
def get_progress(bid):
    p = db.session.execute(db.select(StudentProgress).filter_by(block_id=bid)).scalar_one_or_none()
    if not p: return jsonify({'answers': {}, 'score': 0})
    return jsonify({'answers': json.loads(p.answers), 'score': p.score})

@app.route('/api/progress/<int:bid>', methods=['POST'])
def save_progress(bid):
    d = request.json
    p = db.session.execute(db.select(StudentProgress).filter_by(block_id=bid)).scalar_one_or_none()
    if not p:
        p = StudentProgress(block_id=bid)
        db.session.add(p)
    p.answers = json.dumps(d.get('answers', {}), ensure_ascii=False)
    p.score   = d.get('score', 0)
    db.session.commit()
    return jsonify({'ok': True})

# ─── API: .urok Export ────────────────────────────────────────────────────────

@app.route('/api/urok/encode', methods=['POST'])
def encode_urok_api():
    """Darsni .urok formatiga kodlash (o'qituvchi uchun)"""
    d = request.json or {}
    lesson_id = d.get('lesson_id')
    if not lesson_id:
        return jsonify({'error': 'lesson_id kerak'}), 400
    lesson = db.session.get(Lesson, lesson_id)
    if not lesson:
        return jsonify({'error': 'Dars topilmadi'}), 404
    blocks = db.session.execute(
        db.select(Block).filter_by(lesson_id=lesson_id).order_by(Block.order)
    ).scalars().all()
    payload = {
        'title': lesson.title,
        'subtitle': lesson.subtitle,
        'blocks': [{'type': b.type, 'order': b.order,
                    'data': json.loads(b.data or '{}')} for b in blocks]
    }
    encoded = encode_urok(payload)
    return jsonify({'ok': True, 'data': encoded, 'filename': f'{lesson.title}.urok'})


# ─── API: .urok Import (o'quvchi uchun) ─────────────────────────────────────

@app.route('/api/urok/decode', methods=['POST'])
def decode_urok_api():
    """Frontend .urok faylni yuboradi, JSON payload qaytaradi (o'quvchi rejimi)"""
    f = request.files.get('file')
    if not f:
        # JSON body orqali ham qabul qilish
        d = request.json or {}
        b64 = d.get('data', '')
    else:
        b64 = f.read().decode('ascii').strip()

    try:
        payload = decode_urok(b64)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    return jsonify({'ok': True, 'payload': payload})

@app.route('/api/urok/decode-teacher', methods=['POST'])
def decode_urok_teacher():
    """O'qituvchi uchun: parol tekshirib, keyin decode qiladi"""
    d = request.json or {}
    pwd = d.get('password', '')
    if hashlib.sha256(pwd.encode()).hexdigest() != TEACHER_PASS_HASH:
        return jsonify({'ok': False, 'error': 'Parol noto\'g\'ri'}), 401

    b64 = d.get('data', '')
    try:
        payload = decode_urok(b64)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    return jsonify({'ok': True, 'payload': payload})

# ─── API: Natijalarni saqlash ─────────────────────────────────────────────────

@app.route('/api/results', methods=['POST'])
def save_result():
    """O'quvchi .urok natija faylini serverga yuboradi"""
    d = request.json or {}
    result = StudentResult(
        student_name = d.get('student_name', 'O\'quvchi'),
        lesson_title = d.get('lesson_title', ''),
        total_score  = d.get('total_score', 0),
        max_score    = d.get('max_score', 0),
        answers_json = json.dumps(d.get('answers', {}), ensure_ascii=False),
    )
    db.session.add(result)
    db.session.commit()
    return jsonify({'ok': True, 'id': result.id})

@app.route('/api/results', methods=['GET'])
def get_results():
    """O'qituvchi barcha natijalarni ko'radi"""
    results = db.session.execute(db.select(StudentResult).order_by(StudentResult.submitted_at.desc())).scalars().all()
    data = []
    for r in results:
        answers_raw = json.loads(r.answers_json or '{}')
        # Har bir blok turini qayta ishlash
        processed = {}
        for block_id, block_data in answers_raw.items():
            if isinstance(block_data, dict):
                b_type  = block_data.get('type', '')
                b_title = block_data.get('title', '')
                b_ans   = block_data.get('answers', {})
                b_score = block_data.get('score', 0)
                b_max   = block_data.get('max', 0)
                # vocab_timer va gen_test uchun javob detallari
                if b_type in ('vocab_timer', 'gen_test'):
                    rows = []
                    for q, v in b_ans.items():
                        if isinstance(v, dict):
                            rows.append({
                                'question':  q,
                                'given':     v.get('given', ''),
                                'correct':   v.get('correct', ''),
                                'is_correct':v.get('isCorrect', v.get('correct', '') == v.get('given', '')),
                            })
                    processed[block_id] = {
                        'type': b_type, 'title': b_title,
                        'score': b_score, 'max': b_max,
                        'rows': rows,
                        'answers': b_ans,
                    }
                else:
                    processed[block_id] = block_data
            else:
                processed[block_id] = block_data

        data.append({
            'id':           r.id,
            'student_name': r.student_name,
            'lesson_title': r.lesson_title,
            'total_score':  r.total_score,
            'max_score':    r.max_score,
            'pct':          round(r.total_score / r.max_score * 100) if r.max_score else 0,
            'answers':      processed,
            'submitted_at': r.submitted_at.strftime('%Y-%m-%d %H:%M'),
        })
    return jsonify(data)

@app.route('/api/results/<int:rid>', methods=['DELETE'])
def delete_result(rid):
    r = db.session.get(StudentResult, rid)
    if not r: return jsonify({'error':'not found'}), 404
    db.session.delete(r)
    db.session.commit()
    return jsonify({'ok': True})

# ─── Upload ───────────────────────────────────────────────────────────────────

@app.route('/api/upload/audio', methods=['POST'])
def upload_audio():
    f = request.files.get('file')
    if not f: return jsonify({'error': 'no file'}), 400
    audio_dir = os.path.join(BASE_DIR, 'static', 'audio')
    os.makedirs(audio_dir, exist_ok=True)
    safe_name = os.path.basename(f.filename or 'file')
    fname = f'{datetime.utcnow().timestamp()}_{safe_name}'
    f.save(os.path.join(audio_dir, fname))
    return jsonify({'url': f'/static/audio/{fname}'})

@app.route('/api/upload/image', methods=['POST'])
def upload_image():
    f = request.files.get('file')
    if not f: return jsonify({'error': 'no file'}), 400
    img_dir = os.path.join(BASE_DIR, 'static', 'img')
    os.makedirs(img_dir, exist_ok=True)
    safe_name = os.path.basename(f.filename or 'file')
    fname = f'{datetime.utcnow().timestamp()}_{safe_name}'
    f.save(os.path.join(img_dir, fname))
    return jsonify({'url': f'/static/img/{fname}'})

@app.route('/api/upload/video', methods=['POST'])
def upload_video():
    f = request.files.get('file')
    if not f: return jsonify({'error': 'no file'}), 400
    vid_dir = os.path.join(BASE_DIR, 'static', 'video')
    os.makedirs(vid_dir, exist_ok=True)
    safe_name = os.path.basename(f.filename or 'file')
    fname = f'{datetime.utcnow().timestamp()}_{safe_name}'
    f.save(os.path.join(vid_dir, fname))
    return jsonify({'url': f'/static/video/{fname}'})


# ─── API: HTML + ZIP Export ────────────────────────────────────────────────────

@app.route('/api/export/zip', methods=['POST'])
def export_zip():
    import zipfile as _zf, io, re as _re
    d = request.json or {}
    lesson_ids   = d.get('lesson_ids', [])
    site_title   = d.get('site_title', 'Mening Darslarim')
    dark_theme   = d.get('dark_theme', False)
    hide_answers = d.get('hide_answers', True)
    if not lesson_ids:
        return jsonify({'error': 'Hech qanday dars tanlanmagan'}), 400
    lessons_data = []
    for lid in lesson_ids:
        lesson = db.session.get(Lesson, lid)
        if not lesson: continue
        blocks = db.session.execute(db.select(Block).filter_by(lesson_id=lid).order_by(Block.order)).scalars().all()
        lessons_data.append({
            'id': lesson.id, 'title': lesson.title, 'subtitle': lesson.subtitle,
            'blocks': [{'type':b.type,'order':b.order,'data':json.loads(b.data or '{}')} for b in blocks]
        })
    if not lessons_data:
        return jsonify({'error': 'Darslar topilmadi'}), 404
    media_urls = set()
    url_patt = _re.compile(r'(?:static)/(audio|video|img)/([^"\'> \n]+)')
    for les in lessons_data:
        for blk in les['blocks']:
            for m in url_patt.finditer(json.dumps(blk['data'])):
                media_urls.add(f"static/{m.group(1)}/{m.group(2)}")
    html_content = _build_site_html(lessons_data, site_title, dark_theme, hide_answers)
    buf = io.BytesIO()
    with _zf.ZipFile(buf, 'w', _zf.ZIP_DEFLATED) as zfile:
        zfile.writestr('index.html', html_content.encode('utf-8'))
        for url in media_urls:
            rel_path = 'media/' + url.replace('static/', '', 1)
            abs_path = os.path.join(BASE_DIR, url)
            if os.path.exists(abs_path):
                zfile.write(abs_path, rel_path)
    buf.seek(0)
    safe = ''.join(c if c.isalnum() or c in '-_ ' else '_' for c in site_title)[:40]
    from flask import Response
    return Response(buf.read(), mimetype='application/zip',
                    headers={'Content-Disposition': 'attachment; filename="' + safe + '_sayt.zip"'})

# ════════════════════════════════════════════════════════════
# YANGI API: Users, Groups, Chat, Announce, Schedule,
#            Resources, Attendance, Videos
# ════════════════════════════════════════════════════════════

# ── Users ───────────────────────────────────────────────────
@app.route('/api/users', methods=['GET'])
def get_users():
    users = db.session.execute(db.select(User).order_by(User.id)).scalars().all()
    return jsonify([u.to_dict() for u in users])

@app.route('/api/users', methods=['POST'])
def create_user():
    d = request.json
    if db.session.execute(db.select(User).filter_by(username=d['username'])).scalar_one_or_none():
        return jsonify({'error': 'username exists'}), 400
    u = User(username=d['username'], display=d.get('display',''), role=d.get('role','student'))
    if d.get('password'):
        u.set_password(d['password'])
    db.session.add(u); db.session.commit()
    return jsonify(u.to_dict())

@app.route('/api/users/<int:uid>', methods=['DELETE'])
def delete_user(uid):
    u = db.session.get(User, uid)
    if not u: return jsonify({'error': 'not found'}), 404
    # Bog'liq yozuvlarni tozalash
    db.session.execute(db.delete(ChatMessage).where(
        db.or_(ChatMessage.sender_id == uid, ChatMessage.receiver_id == uid)))
    db.session.execute(db.delete(Attendance).where(Attendance.user_id == uid))
    db.session.execute(db.delete(HomeworkSubmission).where(HomeworkSubmission.student_id == uid))
    db.session.execute(db.delete(TestSession).where(TestSession.student_id == uid))
    db.session.execute(db.delete(FavoriteWord).where(FavoriteWord.user_id == uid))
    db.session.execute(db.delete(VideoProgress).where(VideoProgress.user_id == uid))
    db.session.execute(db.delete(PushSubscription).where(PushSubscription.user_id == uid))
    db.session.execute(db.delete(UserLog).where(UserLog.user_id == uid))
    db.session.delete(u); db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/users/<int:uid>/block', methods=['POST'])
def toggle_block(uid):
    u = db.session.get(User, uid)
    if not u: return jsonify({'error': 'not found'}), 404
    u.blocked = not u.blocked; db.session.commit()
    return jsonify({'blocked': u.blocked})

# ── Groups ──────────────────────────────────────────────────
@app.route('/api/groups', methods=['GET'])
def get_groups():
    groups = db.session.execute(db.select(Group).order_by(Group.id)).scalars().all()
    return jsonify([g.to_dict() for g in groups])

@app.route('/api/groups', methods=['POST'])
def create_group():
    d = request.json
    g = Group(name=d['name'], owner_id=d['owner_id'])
    for mid in d.get('member_ids', []):
        u = db.session.get(User, mid)
        if u: g.members.append(u)
    db.session.add(g); db.session.commit()
    return jsonify(g.to_dict())

@app.route('/api/groups/<int:gid>', methods=['DELETE'])
def delete_group(gid):
    g = db.session.get(Group, gid)
    if not g: return jsonify({'error': 'not found'}), 404
    db.session.delete(g); db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/groups/<int:gid>/members', methods=['POST'])
def add_member(gid):
    g = db.session.get(Group, gid)
    if not g: return jsonify({'error': 'not found'}), 404
    u = db.session.get(User, request.json.get('user_id'))
    if not u: return jsonify({'error': 'user not found'}), 404
    if u not in g.members: g.members.append(u)
    db.session.commit(); return jsonify(g.to_dict())

@app.route('/api/groups/<int:gid>/members/<int:uid>', methods=['DELETE'])
def remove_member(gid, uid):
    g = db.session.get(Group, gid)
    if not g: return jsonify({'error': 'not found'}), 404
    u = db.session.get(User, uid)
    if u and u in g.members: g.members.remove(u)
    db.session.commit(); return jsonify(g.to_dict())

# ── Chat history ────────────────────────────────────────────
@app.route('/api/chat/global', methods=['GET', 'POST'])
def chat_global():
    if request.method == 'POST':
        d = request.json or {}
        sender_id = d.get('sender_id')
        content = d.get('content', '').strip()
        if not sender_id or not content:
            return jsonify({'error': 'sender_id va content kerak'}), 400
        sender = db.session.get(User, sender_id)
        if not sender or sender.blocked:
            return jsonify({'error': 'Ruxsat yo\'q'}), 403
        msg = ChatMessage(sender_id=sender_id, scope='global', content=content)
        db.session.add(msg); db.session.commit()
        socketio.emit('new_message', msg.to_dict(), room='global')
        return jsonify(msg.to_dict())
    # GET
    msgs = db.session.execute(
        db.select(ChatMessage).where(ChatMessage.scope == 'global')
        .order_by(ChatMessage.created_at.desc()).limit(100)
    ).scalars().all()
    return jsonify([m.to_dict() for m in reversed(msgs)])

@app.route('/api/chat/private/<int:a>/<int:b>')
def chat_private(a, b):
    msgs = db.session.execute(
        db.select(ChatMessage).where(
            ChatMessage.scope == 'private',
            db.or_(
                db.and_(ChatMessage.sender_id == a, ChatMessage.receiver_id == b),
                db.and_(ChatMessage.sender_id == b, ChatMessage.receiver_id == a),
            )
        ).order_by(ChatMessage.created_at.desc()).limit(100)
    ).scalars().all()
    return jsonify([m.to_dict() for m in reversed(msgs)])

@app.route('/api/chat/group/<int:gid>')
def chat_group(gid):
    msgs = db.session.execute(
        db.select(ChatMessage).where(
            ChatMessage.scope == 'group', ChatMessage.group_id == gid
        ).order_by(ChatMessage.created_at.desc()).limit(100)
    ).scalars().all()
    return jsonify([m.to_dict() for m in reversed(msgs)])

# ── Announcements ───────────────────────────────────────────
@app.route('/api/announcements', methods=['GET'])
def get_announcements():
    items = db.session.execute(
        db.select(Announcement)
        .order_by(Announcement.important.desc(), Announcement.created_at.desc())
    ).scalars().all()
    return jsonify([a.to_dict() for a in items])

@app.route('/api/announcements', methods=['POST'])
def create_announcement():
    d = request.json
    a = Announcement(author_id=d['author_id'], title=d['title'],
                     body=d.get('body', ''), important=d.get('important', False))
    db.session.add(a); db.session.commit()
    socketio.emit('new_announcement', a.to_dict(), room='global')
    return jsonify(a.to_dict())

@app.route('/api/announcements/<int:aid>', methods=['DELETE'])
def delete_announcement(aid):
    a = db.session.get(Announcement, aid)
    if not a: return jsonify({'error': 'not found'}), 404
    db.session.delete(a); db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/announcements/<int:aid>/important', methods=['POST'])
def toggle_important(aid):
    a = db.session.get(Announcement, aid)
    if not a: return jsonify({'error': 'not found'}), 404
    a.important = not a.important; db.session.commit()
    return jsonify({'important': a.important})

# ── Schedule ────────────────────────────────────────────────
@app.route('/api/schedule', methods=['GET'])
def get_schedule():
    items = db.session.execute(
        db.select(Schedule).order_by(Schedule.date, Schedule.time_start)
    ).scalars().all()
    return jsonify([s.to_dict() for s in items])

@app.route('/api/schedule', methods=['POST'])
def create_schedule():
    d = request.json
    s = Schedule(title=d['title'], event_type=d.get('event_type', 'lesson'),
                 date=d['date'], time_start=d.get('time_start', ''),
                 time_end=d.get('time_end', ''), group_id=d.get('group_id'),
                 created_by=d['created_by'])
    db.session.add(s); db.session.commit()
    return jsonify(s.to_dict())

@app.route('/api/schedule/<int:sid>', methods=['DELETE'])
def delete_schedule(sid):
    s = db.session.get(Schedule, sid)
    if not s: return jsonify({'error': 'not found'}), 404
    db.session.delete(s); db.session.commit()
    return jsonify({'ok': True})

# ── Resources ───────────────────────────────────────────────
@app.route('/api/resources', methods=['GET'])
def get_resources():
    items = db.session.execute(
        db.select(Resource).order_by(Resource.created_at.desc())
    ).scalars().all()
    return jsonify([r.to_dict() for r in items])

@app.route('/api/resources', methods=['POST'])
def create_resource():
    d = request.json
    r = Resource(title=d['title'], res_type=d.get('res_type', 'link'),
                 url=d['url'], description=d.get('description', ''),
                 group_id=d.get('group_id'), uploaded_by=d['uploaded_by'])
    db.session.add(r); db.session.commit()
    return jsonify(r.to_dict())

@app.route('/api/resources/<int:rid>', methods=['DELETE'])
def delete_resource(rid):
    r = db.session.get(Resource, rid)
    if not r: return jsonify({'error': 'not found'}), 404
    db.session.delete(r); db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/upload/resource', methods=['POST'])
def upload_resource():
    f = request.files.get('file')
    if not f: return jsonify({'error': 'no file'}), 400
    res_dir = os.path.join(STATIC, 'resources')
    os.makedirs(res_dir, exist_ok=True)
    safe_name = os.path.basename(f.filename or 'file')
    fname = f'{datetime.utcnow().timestamp()}_{safe_name}'
    f.save(os.path.join(res_dir, fname))
    return jsonify({'url': f'/static/resources/{fname}'})

# ── Attendance ──────────────────────────────────────────────
@app.route('/api/attendance', methods=['GET'])
def get_attendance():
    date = request.args.get('date')
    gid  = request.args.get('group_id', type=int)
    q    = db.select(Attendance)
    if date: q = q.where(Attendance.date == date)
    if gid:  q = q.where(Attendance.group_id == gid)
    return jsonify([a.to_dict() for a in db.session.execute(q).scalars().all()])

@app.route('/api/attendance', methods=['POST'])
def save_attendance():
    for item in request.json:
        ex = db.session.execute(
            db.select(Attendance).where(
                Attendance.user_id  == item['user_id'],
                Attendance.date     == item['date'],
                Attendance.group_id == item.get('group_id'),
            )
        ).scalar_one_or_none()
        if ex:
            ex.status = item.get('status', 'absent')
            ex.note   = item.get('note', '')
        else:
            db.session.add(Attendance(
                user_id=item['user_id'], date=item['date'],
                status=item.get('status', 'absent'), note=item.get('note', ''),
                group_id=item.get('group_id'), marked_by=item.get('marked_by')
            ))
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/attendance/report')
def attendance_report():
    gid     = request.args.get('group_id', type=int)
    g       = db.session.get(Group, gid) if gid else None
    members = g.members if g else db.session.execute(db.select(User)).scalars().all()
    report  = []
    for u in members:
        recs    = db.session.execute(db.select(Attendance).where(Attendance.user_id == u.id)).scalars().all()
        total   = len(recs)
        present = sum(1 for r in recs if r.status in ('present', 'online'))
        report.append({'user_id': u.id, 'username': u.display or u.username,
                       'total': total, 'present': present, 'absent': total - present,
                       'rate': round(present / total * 100, 1) if total else 0})
    return jsonify(report)

# ── Videos ──────────────────────────────────────────────────
@app.route('/api/videos', methods=['GET'])
def get_videos():
    items = db.session.execute(
        db.select(VideoLesson).order_by(VideoLesson.created_at.desc())
    ).scalars().all()
    return jsonify([v.to_dict() for v in items])

@app.route('/api/videos', methods=['POST'])
def create_video():
    d = request.json
    v = VideoLesson(title=d['title'], url=d['url'],
                    thumbnail=d.get('thumbnail', ''), duration=d.get('duration', 0),
                    group_id=d.get('group_id'), uploaded_by=d['uploaded_by'])
    db.session.add(v); db.session.commit()
    return jsonify(v.to_dict())

@app.route('/api/videos/<int:vid>', methods=['DELETE'])
def delete_video(vid):
    v = db.session.get(VideoLesson, vid)
    if not v: return jsonify({'error': 'not found'}), 404
    db.session.delete(v); db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/videos/<int:vid>/progress', methods=['GET'])
def get_video_progress(vid):
    uid = request.args.get('user_id', type=int)
    p   = db.session.execute(
        db.select(VideoProgress).where(VideoProgress.video_id == vid, VideoProgress.user_id == uid)
    ).scalar_one_or_none()
    return jsonify({'position': p.position if p else 0, 'completed': p.completed if p else False})

@app.route('/api/videos/<int:vid>/progress', methods=['POST'])
def save_video_progress(vid):
    d   = request.json; uid = d['user_id']
    p   = db.session.execute(
        db.select(VideoProgress).where(VideoProgress.video_id == vid, VideoProgress.user_id == uid)
    ).scalar_one_or_none()
    if not p:
        p = VideoProgress(video_id=vid, user_id=uid); db.session.add(p)
    p.position  = d.get('position', 0)
    p.completed = d.get('completed', False)
    db.session.commit(); return jsonify({'ok': True})

# ─── Pages ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/sw.js')
def service_worker():
    return send_from_directory(BASE_DIR, 'sw.js',
                               mimetype='application/javascript')

@app.route('/manifest.json')
def manifest():
    return send_from_directory(BASE_DIR, 'manifest.json',
                               mimetype='application/manifest+json')

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(os.path.join(BASE_DIR, 'static'), filename)



@socketio.on('connect')
def on_connect():
    join_room('global')

@socketio.on('join_room')
def on_join(data):
    join_room(data['room'])

@socketio.on('leave_room')
def on_leave(data):
    leave_room(data['room'])

@socketio.on('send_message')
def on_message(data):
    sender = db.session.get(User, data['sender_id'])
    if not sender or sender.blocked:
        return
    scope = data.get('scope', 'global')
    msg = ChatMessage(
        sender_id   = data['sender_id'],
        receiver_id = data.get('receiver_id'),
        group_id    = data.get('group_id'),
        scope       = scope,
        content     = data['content'],
    )
    db.session.add(msg); db.session.commit()
    payload = msg.to_dict()
    if scope == 'global':
        emit('new_message', payload, room='global')
    elif scope == 'group':
        emit('new_message', payload, room=f'group_{data["group_id"]}')
    elif scope == 'private':
        room = f'priv_{min(data["sender_id"], data["receiver_id"])}_{max(data["sender_id"], data["receiver_id"])}'
        emit('new_message', payload, room=room)

# ════════════════════════════════════════════════════════════
# YANGI MODELLAR: Homework, OnlineTest, Favorite, Progress
# ════════════════════════════════════════════════════════════

class Homework(db.Model):
    __tablename__ = 'homework'
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default='')
    due_date    = db.Column(db.String(20), default='')
    group_id    = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True)
    created_by  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    submissions = db.relationship('HomeworkSubmission', backref='homework',
                                  lazy=True, cascade='all, delete-orphan')
    creator     = db.relationship('User', foreign_keys=[created_by], backref='homeworks')

    def to_dict(self):
        return {'id': self.id, 'title': self.title, 'description': self.description,
                'due_date': self.due_date, 'group_id': self.group_id,
                'created_by': self.created_by,
                'creator': self.creator.display or self.creator.username,
                'created_at': self.created_at.strftime('%d.%m.%Y'),
                'submission_count': len(self.submissions)}


class HomeworkSubmission(db.Model):
    __tablename__ = 'homework_submission'
    id          = db.Column(db.Integer, primary_key=True)
    homework_id = db.Column(db.Integer, db.ForeignKey('homework.id'), nullable=False)
    student_id  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    answer      = db.Column(db.Text, default='')
    file_url    = db.Column(db.Text, default='')
    grade       = db.Column(db.Float, nullable=True)
    feedback    = db.Column(db.Text, default='')
    submitted_at= db.Column(db.DateTime, default=datetime.utcnow)
    graded_at   = db.Column(db.DateTime, nullable=True)
    student     = db.relationship('User', foreign_keys=[student_id], backref='hw_submissions')

    def to_dict(self):
        return {'id': self.id, 'homework_id': self.homework_id,
                'student_id': self.student_id,
                'student_name': self.student.display or self.student.username,
                'answer': self.answer, 'file_url': self.file_url,
                'grade': self.grade, 'feedback': self.feedback,
                'submitted_at': self.submitted_at.strftime('%d.%m.%Y %H:%M'),
                'graded': self.grade is not None}


class OnlineTest(db.Model):
    __tablename__ = 'online_test'
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(200), nullable=False)
    questions   = db.Column(db.Text, default='[]')   # JSON array
    duration    = db.Column(db.Integer, default=10)  # minutes
    group_id    = db.Column(db.Integer, db.ForeignKey('group.id'), nullable=True)
    created_by  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    is_active   = db.Column(db.Boolean, default=False)
    started_at  = db.Column(db.DateTime, nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    sessions    = db.relationship('TestSession', backref='online_test',
                                  lazy=True, cascade='all, delete-orphan')
    creator_rel = db.relationship('User', foreign_keys=[created_by], backref='online_tests')

    def to_dict(self):
        return {'id': self.id, 'title': self.title,
                'questions': json.loads(self.questions or '[]'),
                'duration': self.duration, 'group_id': self.group_id,
                'created_by': self.created_by, 'is_active': self.is_active,
                'started_at': self.started_at.isoformat() if self.started_at else None,
                'created_at': self.created_at.strftime('%d.%m.%Y')}


class TestSession(db.Model):
    __tablename__ = 'test_session'
    id          = db.Column(db.Integer, primary_key=True)
    test_id     = db.Column(db.Integer, db.ForeignKey('online_test.id'), nullable=False)
    student_id  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    answers     = db.Column(db.Text, default='{}')
    score       = db.Column(db.Float, default=0)
    max_score   = db.Column(db.Float, default=0)
    finished    = db.Column(db.Boolean, default=False)
    started_at  = db.Column(db.DateTime, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)
    student     = db.relationship('User', foreign_keys=[student_id], backref='test_sessions')

    def to_dict(self):
        return {'id': self.id, 'test_id': self.test_id,
                'student_id': self.student_id,
                'student_name': self.student.display or self.student.username,
                'score': self.score, 'max_score': self.max_score,
                'pct': round(self.score / self.max_score * 100, 1) if self.max_score else 0,
                'finished': self.finished,
                'started_at': self.started_at.strftime('%d.%m.%Y %H:%M'),
                'finished_at': self.finished_at.strftime('%d.%m.%Y %H:%M') if self.finished_at else None}


class FavoriteWord(db.Model):
    __tablename__ = 'favorite_word'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    ru         = db.Column(db.String(200), nullable=False)
    uz         = db.Column(db.String(200), default='')
    audio      = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user       = db.relationship('User', foreign_keys=[user_id], backref='favorites')

    def to_dict(self):
        return {'id': self.id, 'user_id': self.user_id,
                'ru': self.ru, 'uz': self.uz, 'audio': self.audio,
                'created_at': self.created_at.strftime('%d.%m.%Y')}


class PushSubscription(db.Model):
    __tablename__ = 'push_subscription'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    endpoint   = db.Column(db.Text, nullable=False)
    sub_json   = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ════════════════════════════════════════════════════════════
# API: Homework
# ════════════════════════════════════════════════════════════

@app.route('/api/homework', methods=['GET'])
def get_homework():
    gid = request.args.get('group_id', type=int)
    uid = request.args.get('user_id', type=int)
    q   = db.select(Homework).order_by(Homework.created_at.desc())
    if gid: q = q.where(Homework.group_id == gid)
    items = db.session.execute(q).scalars().all()
    result = []
    for hw in items:
        d = hw.to_dict()
        if uid:
            sub = db.session.execute(
                db.select(HomeworkSubmission).where(
                    HomeworkSubmission.homework_id == hw.id,
                    HomeworkSubmission.student_id  == uid)
            ).scalar_one_or_none()
            d['my_submission'] = sub.to_dict() if sub else None
        result.append(d)
    return jsonify(result)

@app.route('/api/homework', methods=['POST'])
def create_homework():
    d = request.json
    hw = Homework(title=d['title'], description=d.get('description',''),
                  due_date=d.get('due_date',''), group_id=d.get('group_id'),
                  created_by=d['created_by'])
    db.session.add(hw); db.session.commit()
    socketio.emit('new_homework', hw.to_dict(), room='global')
    return jsonify(hw.to_dict())

@app.route('/api/homework/<int:hid>', methods=['DELETE'])
def delete_homework(hid):
    hw = db.session.get(Homework, hid)
    if not hw: return jsonify({'error': 'not found'}), 404
    db.session.delete(hw); db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/homework/<int:hid>/submit', methods=['POST'])
def submit_homework(hid):
    d   = request.json
    sub = db.session.execute(
        db.select(HomeworkSubmission).where(
            HomeworkSubmission.homework_id == hid,
            HomeworkSubmission.student_id  == d['student_id'])
    ).scalar_one_or_none()
    if sub:
        sub.answer   = d.get('answer', sub.answer)
        sub.file_url = d.get('file_url', sub.file_url)
    else:
        sub = HomeworkSubmission(homework_id=hid, student_id=d['student_id'],
                                 answer=d.get('answer',''), file_url=d.get('file_url',''))
        db.session.add(sub)
    db.session.commit()
    return jsonify(sub.to_dict())

@app.route('/api/homework/<int:hid>/grade', methods=['POST'])
def grade_homework(hid):
    d   = request.json
    sub = db.session.execute(
        db.select(HomeworkSubmission).where(
            HomeworkSubmission.homework_id == hid,
            HomeworkSubmission.student_id  == d['student_id'])
    ).scalar_one_or_none()
    if not sub: return jsonify({'error': 'submission not found'}), 404
    sub.grade     = d['grade']
    sub.feedback  = d.get('feedback', '')
    sub.graded_at = datetime.utcnow()
    db.session.commit()
    socketio.emit('homework_graded', {
        'student_id': sub.student_id,
        'homework_title': sub.homework.title,
        'grade': sub.grade, 'feedback': sub.feedback
    }, room='global')
    return jsonify(sub.to_dict())

@app.route('/api/homework/<int:hid>/submissions', methods=['GET'])
def get_hw_submissions(hid):
    subs = db.session.execute(
        db.select(HomeworkSubmission).where(HomeworkSubmission.homework_id == hid)
    ).scalars().all()
    return jsonify([s.to_dict() for s in subs])

@app.route('/api/upload/homework', methods=['POST'])
def upload_homework_file():
    f = request.files.get('file')
    if not f: return jsonify({'error': 'no file'}), 400
    hw_dir = os.path.join(STATIC, 'homework')
    os.makedirs(hw_dir, exist_ok=True)
    safe_name = os.path.basename(f.filename or 'file')
    fname = f'{datetime.utcnow().timestamp()}_{safe_name}'
    f.save(os.path.join(hw_dir, fname))
    return jsonify({'url': f'/static/homework/{fname}'})

# ════════════════════════════════════════════════════════════
# API: Online Test
# ════════════════════════════════════════════════════════════

@app.route('/api/online-tests', methods=['GET'])
def get_online_tests():
    gid = request.args.get('group_id', type=int)
    q   = db.select(OnlineTest).order_by(OnlineTest.created_at.desc())
    if gid: q = q.where(OnlineTest.group_id == gid)
    items = db.session.execute(q).scalars().all()
    return jsonify([t.to_dict() for t in items])

@app.route('/api/online-tests', methods=['POST'])
def create_online_test():
    d = request.json
    t = OnlineTest(title=d['title'],
                   questions=json.dumps(d.get('questions', []), ensure_ascii=False),
                   duration=d.get('duration', 10),
                   group_id=d.get('group_id'), created_by=d['created_by'])
    db.session.add(t); db.session.commit()
    return jsonify(t.to_dict())

@app.route('/api/online-tests/<int:tid>', methods=['DELETE'])
def delete_online_test(tid):
    t = db.session.get(OnlineTest, tid)
    if not t: return jsonify({'error': 'not found'}), 404
    db.session.delete(t); db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/online-tests/<int:tid>/start', methods=['POST'])
def start_online_test(tid):
    t = db.session.get(OnlineTest, tid)
    if not t: return jsonify({'error': 'not found'}), 404
    t.is_active  = True
    t.started_at = datetime.utcnow()
    db.session.commit()
    # Broadcast to all students
    socketio.emit('test_started', {
        'test_id': t.id, 'title': t.title,
        'duration': t.duration,
        'started_at': t.started_at.isoformat()
    }, room='global')
    return jsonify(t.to_dict())

@app.route('/api/online-tests/<int:tid>/stop', methods=['POST'])
def stop_online_test(tid):
    t = db.session.get(OnlineTest, tid)
    if not t: return jsonify({'error': 'not found'}), 404
    t.is_active = False
    db.session.commit()
    socketio.emit('test_stopped', {'test_id': t.id}, room='global')
    return jsonify(t.to_dict())

@app.route('/api/online-tests/<int:tid>/submit', methods=['POST'])
def submit_online_test(tid):
    d   = request.json
    t   = db.session.get(OnlineTest, tid)
    if not t: return jsonify({'error': 'not found'}), 404
    qs  = json.loads(t.questions or '[]')
    ans = d.get('answers', {})
    score = 0
    for i, q in enumerate(qs):
        if str(i) in ans and ans[str(i)] == q.get('correct', -1):
            score += 1
    sess = db.session.execute(
        db.select(TestSession).where(
            TestSession.test_id == tid,
            TestSession.student_id == d['student_id'])
    ).scalar_one_or_none()
    if not sess:
        sess = TestSession(test_id=tid, student_id=d['student_id'])
        db.session.add(sess)
    sess.answers    = json.dumps(ans, ensure_ascii=False)
    sess.score      = score
    sess.max_score  = len(qs)
    sess.finished   = True
    sess.finished_at= datetime.utcnow()
    db.session.commit()
    socketio.emit('test_result', sess.to_dict(), room='global')
    return jsonify(sess.to_dict())

@app.route('/api/online-tests/<int:tid>/results', methods=['GET'])
def get_test_results(tid):
    sessions = db.session.execute(
        db.select(TestSession).where(TestSession.test_id == tid)
        .order_by(TestSession.score.desc())
    ).scalars().all()
    return jsonify([s.to_dict() for s in sessions])

# ════════════════════════════════════════════════════════════
# API: Progress Dashboard
# ════════════════════════════════════════════════════════════

@app.route('/api/progress/dashboard')
def progress_dashboard():
    gid     = request.args.get('group_id', type=int)
    g       = db.session.get(Group, gid) if gid else None
    members = g.members if g else db.session.execute(
        db.select(User).where(User.role == 'student')
    ).scalars().all()
    lessons = db.session.execute(db.select(Lesson).order_by(Lesson.order)).scalars().all()

    result = []
    for u in members:
        # StudentResult dan ball hisobi
        results = db.session.execute(
            db.select(StudentResult).where(StudentResult.student_name.in_([u.display, u.username]))
        ).scalars().all()
        total_score = sum(r.total_score for r in results)
        max_score   = sum(r.max_score   for r in results)
        pct         = round(total_score / max_score * 100, 1) if max_score else 0

        # Homework
        hw_subs = db.session.execute(
            db.select(HomeworkSubmission).where(HomeworkSubmission.student_id == u.id)
        ).scalars().all()
        hw_done   = len(hw_subs)
        hw_total  = db.session.execute(db.select(db.func.count()).select_from(Homework)).scalar() or 0
        avg_grade = round(sum(s.grade for s in hw_subs if s.grade is not None) /
                          max(1, sum(1 for s in hw_subs if s.grade is not None)), 1) if hw_subs else 0

        # Online test
        t_sessions = db.session.execute(
            db.select(TestSession).where(TestSession.student_id == u.id, TestSession.finished == True)
        ).scalars().all()
        t_avg = round(
            sum(round(s.score / s.max_score * 100, 1) if s.max_score else 0 for s in t_sessions)
            / max(1, len(t_sessions)), 1
        ) if t_sessions else 0

        # Attendance
        att_recs  = db.session.execute(db.select(Attendance).where(Attendance.user_id == u.id)).scalars().all()
        att_total = len(att_recs)
        att_ok    = sum(1 for r in att_recs if r.status in ('present', 'online'))
        att_pct   = round(att_ok / att_total * 100, 1) if att_total else 0

        result.append({
            'user_id': u.id, 'username': u.display or u.username,
            'lesson_pct': pct, 'total_score': total_score, 'max_score': max_score,
            'hw_done': hw_done, 'hw_total': hw_total, 'avg_grade': avg_grade,
            'test_count': len(t_sessions), 'test_avg_pct': t_avg,
            'att_pct': att_pct, 'att_total': att_total,
        })

    # Sort by lesson_pct desc
    result.sort(key=lambda x: x['lesson_pct'], reverse=True)
    return jsonify(result)

# ════════════════════════════════════════════════════════════
# API: Favorite Words
# ════════════════════════════════════════════════════════════

@app.route('/api/favorites/<int:uid>', methods=['GET'])
def get_favorites(uid):
    favs = db.session.execute(
        db.select(FavoriteWord).where(FavoriteWord.user_id == uid)
        .order_by(FavoriteWord.created_at.desc())
    ).scalars().all()
    return jsonify([f.to_dict() for f in favs])

@app.route('/api/favorites/<int:uid>', methods=['POST'])
def add_favorite(uid):
    d = request.json
    # Duplicate tekshiruv
    ex = db.session.execute(
        db.select(FavoriteWord).where(
            FavoriteWord.user_id == uid,
            FavoriteWord.ru      == d['ru'])
    ).scalar_one_or_none()
    if ex: return jsonify({'already': True, 'item': ex.to_dict()})
    fav = FavoriteWord(user_id=uid, ru=d['ru'], uz=d.get('uz',''), audio=d.get('audio',''))
    db.session.add(fav); db.session.commit()
    return jsonify({'added': True, 'item': fav.to_dict()})

@app.route('/api/favorites/<int:uid>/<int:fid>', methods=['DELETE'])
def remove_favorite(uid, fid):
    fav = db.session.get(FavoriteWord, fid)
    if not fav or fav.user_id != uid: return jsonify({'error': 'not found'}), 404
    db.session.delete(fav); db.session.commit()
    return jsonify({'ok': True})

# ════════════════════════════════════════════════════════════
# API: Push subscription
# ════════════════════════════════════════════════════════════

@app.route('/api/push/subscribe', methods=['POST'])
def push_subscribe():
    d = request.json
    ex = db.session.execute(
        db.select(PushSubscription).where(
            PushSubscription.user_id  == d['user_id'],
            PushSubscription.endpoint == d['endpoint'])
    ).scalar_one_or_none()
    if not ex:
        ps = PushSubscription(user_id=d['user_id'], endpoint=d['endpoint'],
                              sub_json=json.dumps(d.get('subscription', {})))
        db.session.add(ps)
        db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/push/unsubscribe', methods=['POST'])
def push_unsubscribe():
    d  = request.json
    ex = db.session.execute(
        db.select(PushSubscription).where(PushSubscription.user_id == d['user_id'])
    ).scalars().all()
    for ps in ex: db.session.delete(ps)
    db.session.commit()
    return jsonify({'ok': True})

# ─── Entry point ─────────────────────────────────────────────
# ════════════════════════════════════════════════════════════
# YANGI MODELLAR: UserLog, AvatarUpload
# ════════════════════════════════════════════════════════════

class UserLog(db.Model):
    """Foydalanuvchi faoliyat logi — analytics uchun"""
    __tablename__ = 'user_log'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    username   = db.Column(db.String(100), default='')
    action     = db.Column(db.String(50),  nullable=False)   # login|lesson_open|logout
    detail     = db.Column(db.String(200), default='')       # dars nomi, blok turi, etc.
    ip_addr    = db.Column(db.String(50),  default='')
    created_at = db.Column(db.DateTime,    default=datetime.utcnow)

    def to_dict(self):
        return {'id': self.id, 'user_id': self.user_id,
                'username': self.username, 'action': self.action,
                'detail': self.detail,
                'created_at': self.created_at.strftime('%d.%m.%Y %H:%M')}

def _log(user_id, username, action, detail='', req=None):
    """Qisqa log yozuvchi helper"""
    ip = req.remote_addr if req else ''
    db.session.add(UserLog(user_id=user_id, username=username,
                           action=action, detail=detail, ip_addr=ip))
    db.session.commit()

# ════════════════════════════════════════════════════════════
# API: Haqiqiy Login
# ════════════════════════════════════════════════════════════

@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    d        = request.json or {}
    username = d.get('username','').strip().lower()
    password = d.get('password','')

    u = db.session.execute(
        db.select(User).where(db.func.lower(User.username) == username)
    ).scalar_one_or_none()

    if not u:
        return jsonify({'ok': False, 'error': 'Foydalanuvchi topilmadi'}), 401
    if u.blocked:
        return jsonify({'ok': False, 'error': 'Siz bloklangansiz'}), 403
    if not u.password_hash:
        return jsonify({'ok': False, 'error': "Parol o'rnatilmagan. Admin bilan bog'laning."}), 401
    if not u.check_password(password):
        return jsonify({'ok': False, 'error': "Parol noto'g'ri"}), 401

    # Update last_seen
    u.last_seen = datetime.utcnow()
    db.session.commit()
    _log(u.id, u.username, 'login', u.role, request)
    return jsonify({'ok': True, 'user': u.to_dict()})

@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    d = request.json or {}
    uid = d.get('user_id')
    if uid:
        u = db.session.get(User, uid)
        if u:
            _log(u.id, u.username, 'logout', '', request)
    return jsonify({'ok': True})

@app.route('/api/auth/change-password', methods=['POST'])
def change_password():
    d   = request.json or {}
    u   = db.session.get(User, d.get('user_id'))
    if not u: return jsonify({'error': 'not found'}), 404
    old = d.get('old_password','')
    if u.password_hash and not u.check_password(old):
        return jsonify({'ok': False, 'error': "Eski parol noto'g'ri"}), 401
    u.set_password(d.get('new_password',''))
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/auth/set-password', methods=['POST'])
def set_password_admin():
    """O'qituvchi boshqa foydalanuvchiga parol o'rnatadi"""
    d    = request.json or {}
    u    = db.session.get(User, d.get('user_id'))
    if not u: return jsonify({'error': 'not found'}), 404
    u.set_password(d.get('password',''))
    db.session.commit()
    return jsonify({'ok': True})

# ════════════════════════════════════════════════════════════
# API: Avatar
# ════════════════════════════════════════════════════════════

@app.route('/api/users/<int:uid>/avatar', methods=['POST'])
def set_avatar(uid):
    u = db.session.get(User, uid)
    if not u: return jsonify({'error': 'not found'}), 404

    # 1. Emoji avatar
    emoji = (request.json or {}).get('emoji')
    if emoji:
        u.avatar = emoji
        db.session.commit()
        return jsonify({'ok': True, 'avatar': u.avatar})

    # 2. Fayl yuklash
    f = request.files.get('file')
    if f:
        av_dir = os.path.join(STATIC, 'avatars')
        os.makedirs(av_dir, exist_ok=True)
        safe_name = os.path.basename(f.filename or 'avatar')
        ext   = os.path.splitext(safe_name)[1].lower() or '.jpg'
        if ext not in ('.jpg', '.jpeg', '.png', '.gif', '.webp'):
            return jsonify({'error': 'Faqat rasm fayllari ruxsat etilgan'}), 400
        fname = f'user_{uid}{ext}'
        f.save(os.path.join(av_dir, fname))
        u.avatar = f'/static/avatars/{fname}'
        db.session.commit()
        return jsonify({'ok': True, 'avatar': u.avatar})

    return jsonify({'error': 'emoji yoki fayl kerak'}), 400

# ════════════════════════════════════════════════════════════
# API: Qidiruv
# ════════════════════════════════════════════════════════════

@app.route('/api/search')
def search():
    q   = request.args.get('q','').strip().lower()
    uid = request.args.get('user_id', type=int)
    if not q or len(q) < 2:
        return jsonify({'lessons':[], 'words':[], 'resources':[]})

    # Darslar va bloklardagi so'zlar
    lessons  = db.session.execute(db.select(Lesson)).scalars().all()
    lesson_r = []
    word_r   = []

    for les in lessons:
        if q in les.title.lower() or q in les.subtitle.lower():
            lesson_r.append({'type':'lesson','id':les.id,
                             'title':les.title,'subtitle':les.subtitle})
        for blk in les.blocks:
            try:
                data = json.loads(blk.data or '{}')
            except Exception:
                continue
            # vocab bloklar
            if blk.type == 'vocab':
                for item in data.get('items',[]):
                    ru = item.get('ru','').lower()
                    uz = item.get('uz','').lower()
                    if q in ru or q in uz:
                        word_r.append({'type':'word','lesson_id':les.id,
                                       'lesson_title':les.title,
                                       'ru':item.get('ru',''),'uz':item.get('uz',''),
                                       'audio':item.get('audio','')})
            # heading/dialog matnlari
            if blk.type == 'heading' and q in data.get('text','').lower():
                lesson_r.append({'type':'block','id':les.id,'title':les.title,
                                 'subtitle':data.get('text','')})

    # Resurslar
    res_q   = db.select(Resource).where(
        db.or_(Resource.title.ilike(f'%{q}%'),
               Resource.description.ilike(f'%{q}%'))
    )
    res_r = [r.to_dict() for r in db.session.execute(res_q).scalars().all()]

    # Log
    if uid:
        _log(uid, '', 'search', q, request)

    return jsonify({'lessons': lesson_r[:20], 'words': word_r[:30],
                    'resources': res_r[:10]})

# ════════════════════════════════════════════════════════════
# API: Analytics
# ════════════════════════════════════════════════════════════

@app.route('/api/analytics')
def get_analytics():
    days = request.args.get('days', 30, type=int)
    from datetime import timedelta
    since = datetime.utcnow() - timedelta(days=days)

    logs = db.session.execute(
        db.select(UserLog).where(UserLog.created_at >= since)
        .order_by(UserLog.created_at.desc())
    ).scalars().all()

    # Unique foydalanuvchilar (kunlik)
    daily = {}
    for log in logs:
        day = log.created_at.strftime('%Y-%m-%d')
        if day not in daily:
            daily[day] = {'date': day, 'logins': 0, 'actions': 0, 'users': set()}
        daily[day]['actions'] += 1
        if log.action == 'login':
            daily[day]['logins'] += 1
        if log.username:
            daily[day]['users'].add(log.username)

    daily_list = sorted([
        {'date': d['date'], 'logins': d['logins'],
         'actions': d['actions'], 'unique_users': len(d['users'])}
        for d in daily.values()
    ], key=lambda x: x['date'])

    # Eng faol foydalanuvchilar
    from collections import Counter
    user_actions = Counter(l.username for l in logs if l.username)
    top_users = [{'username': u, 'actions': c}
                 for u, c in user_actions.most_common(10)]

    # Eng mashhur darslar
    lesson_opens = Counter(l.detail for l in logs if l.action == 'lesson_open' and l.detail)
    top_lessons  = [{'title': t, 'opens': c}
                    for t, c in lesson_opens.most_common(10)]

    # Action statistikasi
    action_stats = Counter(l.action for l in logs)

    # Oxirgi 20 log
    recent = [l.to_dict() for l in logs[:20]]

    return jsonify({
        'total_logs': len(logs),
        'daily': daily_list,
        'top_users': top_users,
        'top_lessons': top_lessons,
        'action_stats': dict(action_stats),
        'recent': recent,
    })

@app.route('/api/analytics/log', methods=['POST'])
def log_action():
    """Frontend dan action logini qabul qilish"""
    d = request.json or {}
    _log(d.get('user_id'), d.get('username',''), d.get('action','view'),
         d.get('detail',''), request)
    return jsonify({'ok': True})

# ─── Entry point ─────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    print('\n🚀  LangLearn ishga tushdi!  →  http://127.0.0.1:5000\n')
    socketio.run(app, debug=True, port=5000)

def _build_site_html(lessons_data, site_title, dark_theme, hide_answers):
    """Darslardan to'liq offline HTML sayt yasaydi"""
    import json as _json, re as _re, random as _random

    def fix_url(s):
        if not s: return s
        return _re.sub(r'(?:/static/|static/)(audio|video|img)/([^"\'>\s]+)',
                       r'media/\1/\2', str(s))

    def esc(s):
        return str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')

    def build_block_html(blk):
        t = blk['type']
        d = blk['data']

        if t == 'heading':
            sizes = {'1':'2rem','2':'1.6rem','3':'1.3rem','4':'1.1rem'}
            sz = sizes.get(str(d.get('level',1)),'2rem')
            lv = d.get('level',1)
            return f'<h{lv} class="s-heading" style="font-size:{sz};color:{esc(d.get("color","#e63946"))}">{esc(d.get("text",""))}</h{lv}>\n'

        if t == 'hr':
            return f'<hr style="height:{d.get("height",3)}px;background:{esc(d.get("color","#e63946"))};border:none;border-radius:2px;margin:12px 0">\n'

        if t == 'image':
            url = fix_url(d.get('url',''))
            if not url: return ''
            cap = esc(d.get('caption',''))
            w = d.get('width',100)
            return f'<div class="media-block"><img src="{esc(url)}" style="width:{w}%;border-radius:10px;max-width:100%" alt="{cap}">{f"<p class=media-caption>{cap}</p>" if cap else ""}</div>\n'

        if t == 'audio_text':
            au = fix_url(d.get('audio',''))
            tx = esc(d.get('text',''))
            atag = f'<audio controls src="{esc(au)}" style="width:100%;margin-bottom:12px"></audio>' if au else ''
            return f'<div class="block-card">{atag}<div style="font-size:15px;line-height:1.8">{tx}</div></div>\n'

        if t == 'media':
            mt = d.get('type','audio')
            url = fix_url(d.get('url',''))
            title = esc(d.get('title',''))
            cap = esc(d.get('caption',''))
            icon = '▶️' if mt=='youtube' else ('🎬' if mt=='video' else '🎵')
            hdr = f'<div class="sec-bar" style="background:#0f766e">{icon} {title}</div>' if title else ''
            if mt == 'youtube':
                yt = _re.search(r'(?:v=|youtu\.be/|embed/)([\w-]{11})', url or '')
                yid = yt.group(1) if yt else url
                inner = f'<div style="position:relative;padding-bottom:56.25%;height:0;overflow:hidden;border-radius:10px"><iframe src="https://www.youtube.com/embed/{yid}" style="position:absolute;top:0;left:0;width:100%;height:100%;border:none" allowfullscreen></iframe></div>'
            elif mt == 'video':
                inner = f'<video controls src="{esc(url)}" style="width:100%;border-radius:10px;background:#000"></video>' if url else ''
            else:
                inner = f'<audio controls src="{esc(url)}" style="width:100%"></audio>' if url else ''
            return f'<div class="block-card">{hdr}{inner}{f"<p class=media-caption>{cap}</p>" if cap else ""}</div>\n'

        if t == 'vocab':
            items = d.get('items',[])
            bar = esc(d.get('bar_color','#457b9d'))
            title = esc(d.get('title',"So'zlar"))
            rows = ''
            for i,item in enumerate(items):
                au = fix_url(item.get('audio',''))
                abtn = f'<button class="play-btn" onclick="playAudio(\'{esc(au)}\')">🔊</button>' if au else ''
                rows += f'<div class="vocab-item"><span class="vnum">{i+1}</span><span class="vru">{esc(item.get("ru",""))}</span><span class="vuz">— {esc(item.get("uz",""))}</span>{abtn}</div>'
            return f'<div class="block-card"><div class="sec-bar" style="background:{bar}">📖 {title}</div><div class="vocab-grid">{rows}</div></div>\n'

        if t == 'dialog':
            lines = d.get('lines',[])
            title = esc(d.get('title','Dialog'))
            rows = ''
            for l in lines:
                sp = l.get('speaker','A')
                rows += f'<div class="d-line"><span class="d-sp {esc(sp)}">{esc(sp)}</span><span class="d-txt">{esc(l.get("text",""))}</span></div>'
            return f'<div class="block-card"><div class="d-title">💬 {title}</div>{rows}</div>\n'

        if t == 'fill_blank':
            items = d.get('items',[])
            title = esc(d.get('title','Mashq'))
            instr = esc(d.get('instruction',''))
            bar = esc(d.get('bar_color','#2a9d8f'))
            rows = ''
            for i,item in enumerate(items):
                ans = esc(item.get('answer',''))
                pre = esc(item.get('pre',''))
                post = esc(item.get('post',''))
                if hide_answers:
                    inp = f'<input class="blank-inp" data-answer="{ans}" placeholder="...">'
                else:
                    inp = f'<span class="blank-answer">{ans}</span>'
                rows += f'<div class="fb-row"><span class="fb-num">{i+1}</span>{pre} {inp} {post}</div>'
            chk = '<button class="check-btn" onclick="checkFillBlank(this)">✔ Tekshirish</button><div class="fb-score"></div>' if hide_answers else ''
            return f'<div class="block-card"><div class="sec-bar" style="background:{bar}">✏️ {title}</div>{f"<p class=instruction>{instr}</p>" if instr else ""}<div class="fb-list">{rows}</div>{chk}</div>\n'

        if t == 'match':
            pairs = d.get('pairs',[])
            title = esc(d.get('title','Moslashtirish'))
            bar = esc(d.get('bar_color','#e76f51'))
            left = ''.join(f'<div class="match-item" data-idx="{i}" data-side="left">{esc(p.get("left",""))}</div>' for i,p in enumerate(pairs))
            shuffled = pairs[:]
            _random.shuffle(shuffled)
            right = ''.join(f'<div class="match-item" data-orig="{next((j for j,p in enumerate(pairs) if p.get("right")==s.get("right")),0)}" data-side="right">{esc(s.get("right",""))}</div>' for s in shuffled)
            return f'<div class="block-card"><div class="sec-bar" style="background:{bar}">🔗 {title}</div><div class="match-grid"><div class="match-col">{left}</div><div class="match-col">{right}</div></div><div class="match-score"></div></div>\n'

        if t == 'quiz':
            qs = d.get('questions',[])
            title = esc(d.get('title','Test'))
            bar = esc(d.get('bar_color','#6a4c93'))
            inner = ''
            for qi,q in enumerate(qs):
                opts = ''.join(f'<button class="quiz-opt" onclick="answerQuiz(this,{oi},{q.get("correct",0)},\'q{qi}_{id(qs)}\')">{esc(o)}</button>' for oi,o in enumerate(q.get('options',[])))
                inner += f'<div class="quiz-q" id="q{qi}_{id(qs)}"><div class="q-text">{qi+1}. {esc(q.get("q",""))}</div><div class="quiz-opts">{opts}</div></div>'
            return f'<div class="block-card"><div class="sec-bar" style="background:{bar}">🧠 {title}</div><div class="quiz-inner">{inner}</div></div>\n'

        if t == 'table':
            hdrs = d.get('headers',[])
            rows = d.get('rows',[])
            title = esc(d.get('title',''))
            th = ''.join(f'<th>{esc(h)}</th>' for h in hdrs)
            tbody = ''.join(f'<tr>{"".join(f"<td>{esc(c)}</td>" for c in r)}</tr>' for r in rows)
            hdr = f'<div class="sec-bar" style="background:#1d3557">📊 {title}</div>' if title else ''
            return f'<div class="block-card">{hdr}<div style="overflow-x:auto"><table class="s-table"><thead><tr>{th}</tr></thead><tbody>{tbody}</tbody></table></div></div>\n'

        if t == 'vocab_timer':
            items = d.get('items',[])
            title = esc(d.get('title','Lug\'at'))
            tsec = int(d.get('timer_sec',60))
            rows = ''.join(f'<div class="vocab-item"><span class="vnum">{i+1}</span><span class="vru">{esc(it.get("ru",""))}</span><span class="vuz">— {esc(it.get("uz",""))}</span></div>' for i,it in enumerate(items))
            return f'<div class="block-card"><div class="sec-bar" style="background:#0ea5e9">⏱ {title}</div><p style="font-size:13px;color:#6c757d;margin-bottom:8px">Yodlash uchun: {tsec} soniya</p><div class="vocab-grid">{rows}</div></div>\n'

        if t == 'dictation':
            sents = d.get('sentences',[])
            title = esc(d.get('title','Diktant'))
            spd = d.get('speed',0.8)
            lang = d.get('lang','ru')
            inner = ''
            for i,s in enumerate(sents):
                txt = esc(s.get('text',''))
                hint = esc(s.get('hint',''))
                if hide_answers:
                    inner += f'<div class="dict-row"><span class="dict-num">{i+1}</span><button class="play-btn" onclick="ttsPlay(\'{s.get("text","")}\',\'{lang}\',{spd})">▶ Eshit</button><textarea class="dict-inp" rows="2" placeholder="Eshitib yozing..."></textarea><div class="dict-ans" data-answer="{txt}" style="display:none"></div>{f"<div class=hint>💡 {hint}</div>" if hint else ""}<button class="check-btn" onclick="checkDictRow(this)">✔</button><div class="dict-fb"></div></div>'
                else:
                    inner += f'<div class="dict-row"><span class="dict-num">{i+1}</span><div class="dict-show">{txt}</div></div>'
            return f'<div class="block-card"><div class="sec-bar" style="background:#0369a1">🎧 {title}</div>{inner}</div>\n'

        if t == 'memory_chain':
            items = d.get('items',[])
            title = esc(d.get('title','Zanjir xotira'))
            tsec = int(d.get('show_sec',3))
            chips = ''.join(f'<span class="chain-chip">{esc(it)}</span>' for it in items if it)
            uid = str(abs(hash(str(items))))
            return f'<div class="block-card"><div class="sec-bar" style="background:#7c3aed">🔗 {title}</div><p style="font-size:13px;color:#6c757d">Har element {tsec} soniya ko\'rsatiladi.</p><div class="chain-chips" id="cc{uid}">{chips}</div><button class="check-btn" onclick="startChain(this,{tsec},{uid})">▶ Boshlash</button><div class="chain-area" id="ca{uid}" style="display:none"><textarea class="chain-inp" rows="2" placeholder="Elementlarni vergul bilan..."></textarea><button class="check-btn" onclick="checkChain(this)">✔ Tekshirish</button><div class="chain-fb"></div></div></div>\n'

        if t == 'role_dialog':
            turns = d.get('turns',[])
            title = esc(d.get('title','Rol dialog'))
            scen = esc(d.get('scenario',''))
            lang = d.get('lang','uz')
            srole = esc(d.get('student_role',"O'quvchi"))
            prole = esc(d.get('ai_role','Sherik'))
            inner = ''
            for turn in turns:
                role = turn.get('role','partner')
                if role == 'partner':
                    txt = esc(turn.get('text',''))
                    inner += f'<div class="rd-partner"><span class="rd-avatar">🤝</span><div class="rd-bubble partner">{txt} <button class="play-btn" onclick="ttsPlay(\'{turn.get("text","")}\',\'{lang}\',0.85)">🔊</button></div></div>'
                else:
                    prompt = esc(turn.get('prompt',''))
                    inner += f'<div class="rd-student"><div class="rd-prompt">{f"<div class=hint>💡 {prompt}</div>" if prompt else ""}<textarea class="rd-inp" rows="2" placeholder="Sizning javobingiz..."></textarea><button class="play-btn" onclick="ttsPlay(this.previousElementSibling.value,\'{lang}\',0.9)">🔊</button></div></div>'
            return f'<div class="block-card"><div class="sec-bar" style="background:#be123c">🎭 {title}</div>{f"<div class=scenario>{scen}</div>" if scen else ""}<div style="display:flex;gap:10px;margin-bottom:12px"><span class="role-badge-s">👨‍🎓 {srole}</span><span class="role-badge-p">🤝 {prole}</span></div><div class="rd-dialog">{inner}</div></div>\n'

        if t == 'memory_game':
            pairs = d.get('pairs',[])
            title = esc(d.get('title',"Xotira o'yini"))
            uid = str(abs(hash(str(pairs))))
            pairs_json = _json.dumps(pairs).replace("'", "\\'")
            return f'<div class="block-card"><div class="sec-bar" style="background:#9333ea">🃏 {title}</div><p style="font-size:13px;color:#6c757d;margin-bottom:12px">{len(pairs)} juft — bosib juftlarini toping!</p><div class="mg-grid" id="mg{uid}"></div><script>initMemGame(document.getElementById("mg{uid}"),{_json.dumps(pairs)});</script></div>\n'

        if t == 'number_guess':
            mn = int(d.get('min',1)); mx = int(d.get('max',100))
            lng = d.get('lang','uz')
            title = esc(d.get('title','Son topish'))
            hint = f"{mn} dan {mx} gacha son o'yladim!" if lng=='uz' else f"Я загадал число от {mn} до {mx}!"
            uid = str(abs(hash(title+str(mn)+str(mx))))
            return f'<div class="block-card"><div class="sec-bar" style="background:#ea580c">🔢 {title}</div><p class="ng-hint">{hint}</p><div class="ng-wrap"><input id="ngi{uid}" type="number" min="{mn}" max="{mx}" class="ng-inp" placeholder="Son..." onkeydown="if(event.key===\'Enter\')ngCheck(\'{uid}\',{mn},{mx},\'{lng}\')"><button class="check-btn" onclick="ngCheck(\'{uid}\',{mn},{mx},\'{lng}\')">Tekshirish</button></div><div class="ng-history" id="ngh{uid}"></div><div class="ng-msg" id="ngm{uid}"></div><script>window._ng_{uid}=Math.floor(Math.random()*({mx}-{mn}+1))+{mn};</script></div>\n'

        if t == 'anagram':
            words = [w for w in d.get('words',[]) if w.get('word')]
            title = esc(d.get('title','Anagram'))
            uid = str(abs(hash(str(words))))
            return f'<div class="block-card"><div class="sec-bar" style="background:#16a34a">🧩 {title}</div><div class="an-inner" id="an{uid}" data-words=\'{_json.dumps(words)}\' data-lang="{d.get("lang","uz")}"><button class="check-btn" onclick="initAnagramInline(document.getElementById(\'an{uid}\'))">▶ Boshlash</button></div></div>\n'

        if t == 'flash_cards':
            cards = [c for c in d.get('cards',[]) if c.get('front')]
            title = esc(d.get('title','Tez xotira'))
            fsec = int(d.get('flash_sec',2))
            uid = str(abs(hash(str(cards))))
            return f'<div class="block-card"><div class="sec-bar" style="background:#b45309">⚡ {title}</div><p style="font-size:13px;color:#6c757d;margin-bottom:10px">Karta {fsec} soniya ko\'rsatiladi.</p><div class="fc-inner" id="fc{uid}" data-cards=\'{_json.dumps(cards)}\' data-sec="{fsec}"><button class="check-btn" onclick="initFlashInline(document.getElementById(\'fc{uid}\'))">▶ Boshlash</button></div></div>\n'

        return ''

    # Barcha darslar
    lessons_html = ''
    for les in lessons_data:
        lid = les['id']
        blist = ''.join(build_block_html(b) for b in sorted(les['blocks'], key=lambda x: x['order']))
        lessons_html += f'<section class="lesson-section" id="lesson-{lid}">{blist}</section>\n'

    nav_items = ''
    for i,les in enumerate(lessons_data):
        sub = f'<small>{esc(les["subtitle"])}</small>' if les.get('subtitle') else ''
        nav_items += f'<a class="nav-item" href="#lesson-{les["id"]}" onclick="showLesson({les["id"]});return false"><span class="nav-num">{i+1}</span><span class="nav-txt">{esc(les["title"])}{sub}</span></a>'

    # CSS
    bg = '#12121f' if dark_theme else '#f0f4f8'
    txt = '#e0deef' if dark_theme else '#212529'
    card = '#1c1a2e' if dark_theme else '#ffffff'
    sb = '#0d0d1a' if dark_theme else '#1d3557'
    brd = '#2d2b45' if dark_theme else '#dee2e6'

    css = f"""
:root{{--bg:{bg};--txt:{txt};--card:{card};--sb:{sb};--brd:{brd};
  --green:#2a9d8f;--blue:#457b9d;--red:#e63946;--gray:#6c757d;--r:10px;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--txt);min-height:100vh;display:flex;}}
#sidebar{{width:270px;min-height:100vh;background:var(--sb);color:#fff;display:flex;flex-direction:column;transition:width .3s;position:sticky;top:0;height:100vh;overflow:hidden;flex-shrink:0;z-index:100;}}
#sidebar.collapsed{{width:52px;}}
#sb-head{{padding:14px 12px;display:flex;align-items:center;gap:10px;border-bottom:1px solid rgba(255,255,255,.1);flex-shrink:0;min-height:52px;}}
#sb-toggle{{background:none;border:none;color:#fff;cursor:pointer;font-size:20px;padding:4px 6px;border-radius:6px;flex-shrink:0;transition:background .15s;}}
#sb-toggle:hover{{background:rgba(255,255,255,.15);}}
#sb-title{{font-weight:700;font-size:14px;white-space:nowrap;overflow:hidden;transition:opacity .2s,width .2s;}}
#sidebar.collapsed #sb-title{{opacity:0;width:0;pointer-events:none;}}
#nav-list{{overflow-y:auto;flex:1;padding:6px;}}
.nav-item{{display:flex;gap:8px;align-items:center;padding:10px 10px;color:rgba(255,255,255,.8);text-decoration:none;font-size:13px;border-radius:8px;margin-bottom:3px;transition:all .15s;overflow:hidden;}}
.nav-item:hover,.nav-item.active{{background:rgba(255,255,255,.15);color:#fff;}}
.nav-num{{background:rgba(255,255,255,.2);border-radius:6px;min-width:24px;height:24px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;flex-shrink:0;}}
.nav-txt{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;transition:opacity .2s;}}
.nav-txt small{{display:block;font-size:11px;opacity:.6;}}
#sidebar.collapsed .nav-txt{{opacity:0;width:0;pointer-events:none;}}
#main-content{{flex:1;overflow-y:auto;padding:24px;max-width:860px;margin:0 auto;width:100%;}}
.lesson-section{{display:none;animation:fi .3s ease;}}
.lesson-section.active{{display:block;}}
@keyframes fi{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:none}}}}
.block-card{{background:var(--card);border-radius:var(--r);border:1.5px solid var(--brd);box-shadow:0 2px 12px rgba(0,0,0,.08);padding:20px;margin-bottom:16px;overflow:hidden;}}
.sec-bar{{color:#fff;margin:-20px -20px 14px;padding:10px 18px;font-size:14px;font-weight:700;border-radius:8px 8px 0 0;}}
.s-heading{{font-weight:900;line-height:1.2;margin-bottom:8px;}}
.vocab-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;}}
.vocab-item{{display:flex;align-items:center;gap:8px;padding:8px 12px;border:1.5px solid var(--brd);border-radius:8px;background:var(--card);font-size:14px;}}
.vnum{{background:var(--blue);color:#fff;border-radius:4px;min-width:22px;text-align:center;font-size:11px;font-weight:700;padding:1px 4px;}}
.vru{{font-weight:600;flex:1;}}.vuz{{color:var(--gray);font-size:13px;}}
.d-line{{display:flex;gap:8px;margin-bottom:8px;align-items:flex-start;}}
.d-sp{{font-size:11px;font-weight:700;color:#fff;border-radius:4px;padding:2px 6px;flex-shrink:0;margin-top:2px;}}
.d-sp.A{{background:var(--blue)}}.d-sp.B{{background:var(--green)}}
.d-txt{{font-size:14px;line-height:1.5;}} .d-title{{font-weight:700;margin-bottom:10px;}}
.fb-list{{display:flex;flex-direction:column;gap:8px;}}
.fb-row{{display:flex;align-items:center;flex-wrap:wrap;gap:6px;font-size:15px;}}
.fb-num{{background:var(--green);color:#fff;border-radius:4px;padding:1px 6px;font-size:12px;font-weight:700;flex-shrink:0;}}
.blank-inp{{border:none;border-bottom:2px solid var(--blue);padding:2px 6px;font-size:15px;min-width:80px;text-align:center;background:transparent;font-family:inherit;outline:none;color:inherit;}}
.blank-inp.correct{{border-color:var(--green);color:var(--green)}}.blank-inp.wrong{{border-color:var(--red);color:var(--red)}}
.blank-answer{{font-weight:700;color:var(--green);border-bottom:2px solid var(--green);padding:0 6px;}}
.instruction{{font-size:13px;color:var(--gray);font-style:italic;margin-bottom:10px;}}
.fb-score,.match-score{{font-size:13px;font-weight:600;margin-top:8px;color:var(--green);}}
.match-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px;}}
.match-col{{display:flex;flex-direction:column;gap:6px;}}
.match-item{{padding:9px 12px;border-radius:8px;font-size:14px;cursor:pointer;border:2px solid var(--brd);text-align:center;font-weight:600;transition:all .15s;user-select:none;background:var(--card);}}
.match-item:hover{{border-color:var(--blue);}}.match-item.selected{{border-color:#f59e0b;background:rgba(245,158,11,.1);}}
.match-item.matched{{border-color:var(--green);background:rgba(42,157,143,.1);cursor:default;opacity:.7;}}
.match-item.wrong-flash{{border-color:var(--red);background:rgba(239,68,68,.08);}}
.quiz-q{{margin-bottom:14px;}}.q-text{{font-size:15px;font-weight:700;margin-bottom:8px;}}
.quiz-opts{{display:flex;flex-direction:column;gap:6px;}}
.quiz-opt{{padding:10px 14px;border-radius:8px;border:2px solid var(--brd);font-size:14px;cursor:pointer;text-align:left;background:var(--card);transition:all .15s;color:inherit;}}
.quiz-opt:hover:not(:disabled){{border-color:var(--blue);}}.quiz-opt.correct{{border-color:var(--green);background:rgba(42,157,143,.1);color:var(--green);font-weight:600;}}
.quiz-opt.wrong{{border-color:var(--red);background:rgba(239,68,68,.08);color:var(--red);}}
.s-table{{width:100%;border-collapse:collapse;font-size:13px;}}
.s-table th{{background:#1d3557;color:#fff;padding:8px 12px;text-align:left;}}
.s-table td{{padding:8px 12px;border-bottom:1px solid var(--brd);}}
.check-btn{{padding:8px 18px;border-radius:8px;background:var(--green);color:#fff;border:none;cursor:pointer;font-size:13px;font-weight:600;margin-top:8px;transition:filter .15s;}}
.check-btn:hover{{filter:brightness(1.1);}} .check-btn:disabled{{opacity:.5;cursor:not-allowed;}}
.play-btn{{padding:4px 10px;border-radius:6px;background:var(--blue);color:#fff;border:none;cursor:pointer;font-size:12px;}}
.media-block{{text-align:center;margin-bottom:16px;}} .media-caption{{font-size:12px;color:var(--gray);margin-top:6px;font-style:italic;}}
.dict-row{{display:flex;align-items:flex-start;gap:8px;margin-bottom:12px;flex-wrap:wrap;}}
.dict-num{{background:var(--blue);color:#fff;border-radius:4px;min-width:24px;height:24px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;flex-shrink:0;margin-top:4px;}}
.dict-inp{{flex:1;min-width:160px;padding:8px;border:1.5px solid var(--brd);border-radius:8px;font-size:14px;font-family:inherit;resize:vertical;min-height:40px;background:var(--card);color:inherit;}}
.hint{{font-size:12px;color:var(--gray);font-style:italic;width:100%;padding-left:0;}} .dict-show{{font-size:14px;line-height:1.6;}}
.chain-chips{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;}}
.chain-chip{{padding:6px 14px;border-radius:20px;background:#7c3aed;color:#fff;font-size:14px;font-weight:600;}}
.chain-inp{{width:100%;padding:9px;border:1.5px solid var(--brd);border-radius:8px;font-size:14px;font-family:inherit;resize:vertical;min-height:42px;background:var(--card);color:inherit;margin-bottom:4px;}}
.scenario{{background:rgba(190,18,60,.08);border-left:4px solid #be123c;padding:10px 14px;border-radius:0 8px 8px 0;font-size:14px;font-style:italic;margin-bottom:12px;}}
.rd-dialog{{display:flex;flex-direction:column;gap:10px;}} .rd-partner,.rd-student{{display:flex;gap:8px;align-items:flex-start;}}
.rd-student{{flex-direction:row-reverse;}} .rd-avatar{{width:32px;height:32px;border-radius:50%;background:#7c3aed;color:#fff;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0;}}
.rd-bubble{{max-width:72%;padding:10px 14px;border-radius:12px;font-size:14px;line-height:1.5;background:rgba(124,58,237,.1);border:1px solid rgba(124,58,237,.2);}}
.rd-inp{{width:100%;padding:8px;border:1.5px solid var(--brd);border-radius:8px;font-size:14px;font-family:inherit;resize:vertical;min-height:44px;background:var(--card);color:inherit;}}
.rd-prompt{{flex:1;display:flex;flex-direction:column;align-items:flex-end;}}
.role-badge-s{{padding:3px 10px;border-radius:20px;background:rgba(190,18,60,.1);color:#be123c;font-size:12px;font-weight:600;}}
.role-badge-p{{padding:3px 10px;border-radius:20px;background:rgba(124,58,237,.1);color:#7c3aed;font-size:12px;font-weight:600;}}
.ng-hint{{font-size:14px;color:#ea580c;font-weight:600;margin-bottom:12px;}}
.ng-wrap{{display:flex;gap:8px;align-items:center;}}
.ng-inp{{padding:9px 14px;border:2px solid #ea580c;border-radius:8px;font-size:16px;font-weight:700;text-align:center;width:120px;font-family:inherit;outline:none;background:var(--card);color:inherit;}}
.ng-history{{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px;}}
.mg-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:8px;}}
.mg-card{{height:72px;border-radius:10px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:700;border:2px solid var(--brd);background:var(--card);transition:all .2s;user-select:none;}}
.mg-card.open{{border-color:#9333ea;background:rgba(147,51,234,.1);color:#9333ea;}}
.mg-card.matched{{border-color:var(--green);background:rgba(42,157,143,.1);color:var(--green);cursor:default;}}
@media(max-width:640px){{
  body{{flex-direction:column;}}
  #sidebar{{width:100%;min-height:auto;height:auto;position:static;}}
  #sidebar.collapsed{{width:100%;height:52px;}}
  #nav-list{{display:flex;overflow-x:auto;padding:4px 6px;}}
  .nav-item{{flex-shrink:0;}}
  #main-content{{padding:12px;}}
}}"""

    js = """
// Sidebar toggle
(function(){
  var sb=document.getElementById('sidebar');
  var btn=document.getElementById('sb-toggle');
  function restore(){var v=localStorage.getItem('sb_c');if(v==='1'){sb.classList.add('collapsed');btn.textContent='☰';}}
  btn.addEventListener('click',function(){sb.classList.toggle('collapsed');btn.textContent=sb.classList.contains('collapsed')?'☰':'✕';localStorage.setItem('sb_c',sb.classList.contains('collapsed')?'1':'0');});
  restore();
})();

function showLesson(id){
  document.querySelectorAll('.lesson-section').forEach(function(s){s.classList.remove('active');});
  var sec=document.getElementById('lesson-'+id);
  if(sec)sec.classList.add('active');
  document.querySelectorAll('.nav-item').forEach(function(a){a.classList.toggle('active',a.getAttribute('href')==='#lesson-'+id);});
  localStorage.setItem('al',id);
  window.scrollTo({top:0,behavior:'smooth'});
}

// Restore
(function(){
  var saved=localStorage.getItem('al');
  var all=document.querySelectorAll('.lesson-section');
  if(!all.length)return;
  var shown=false;
  all.forEach(function(s){if(s.id==='lesson-'+saved){s.classList.add('active');shown=true;}});
  if(!shown&&all[0])all[0].classList.add('active');
  var active=document.querySelector('.lesson-section.active');
  if(active){document.querySelectorAll('.nav-item').forEach(function(a){a.classList.toggle('active',a.getAttribute('href')==='#'+active.id);});}
})();

function ttsPlay(text,lang,rate){if(!text||!window.speechSynthesis)return;window.speechSynthesis.cancel();var u=new SpeechSynthesisUtterance(text);u.lang=lang==='ru'?'ru-RU':'uz-UZ';u.rate=rate||0.9;var v=window.speechSynthesis.getVoices().find(function(v){return v.lang.startsWith(lang==='ru'?'ru':'uz');});if(v)u.voice=v;window.speechSynthesis.speak(u);}
function playAudio(src){try{new Audio(src).play();}catch(e){}}

function checkFillBlank(btn){var card=btn.closest('.block-card');var c=0,t=0;card.querySelectorAll('.blank-inp').forEach(function(inp){var a=inp.dataset.answer.trim().toLowerCase();inp.classList.remove('correct','wrong');if(inp.value.trim().toLowerCase()===a){inp.classList.add('correct');c++;}else{inp.classList.add('wrong');}t++;});var s=card.querySelector('.fb-score');if(s)s.textContent='Natija: '+c+'/'+t+' ('+(t?Math.round(c/t*100):0)+'%)';}

function checkDictRow(btn){var row=btn.closest('.dict-row');var inp=row.querySelector('.dict-inp');var ans=row.querySelector('.dict-ans');if(!inp||!ans)return;var fb=row.querySelector('.dict-fb');var ok=inp.value.trim().toLowerCase()===ans.dataset.answer.trim().toLowerCase();inp.style.borderColor=ok?'#2a9d8f':'#e63946';if(fb)fb.innerHTML=ok?'<span style="color:#2a9d8f;font-weight:700">✅ To\\'g\\'ri!</span>':'<span style="color:#e63946">❌ To\\'g\\'ri: <b>'+ans.dataset.answer+'</b></span>';btn.disabled=true;}

document.addEventListener('click',function(e){
  var item=e.target.closest('.match-item');
  if(!item||item.classList.contains('matched'))return;
  var card=item.closest('.block-card');
  var sel=card.querySelector('.match-item.selected');
  if(!sel){item.classList.add('selected');return;}
  if(sel===item){sel.classList.remove('selected');return;}
  if(item.dataset.side===sel.dataset.side){sel.classList.remove('selected');item.classList.add('selected');return;}
  var li=sel.dataset.side==='left'?sel:item;
  var ri=sel.dataset.side==='right'?sel:item;
  if(parseInt(li.dataset.idx)===parseInt(ri.dataset.orig)){
    li.classList.remove('selected');li.classList.add('matched');ri.classList.add('matched');
    var sc=card.querySelector('.match-score');
    var total=card.querySelectorAll('.match-item[data-side="left"]').length;
    var done=card.querySelectorAll('.match-item.matched').length/2;
    if(sc)sc.textContent='✅ '+done+'/'+total+' juft'+(done===total?' 🎉':'');
  }else{
    sel.classList.add('wrong-flash');item.classList.add('wrong-flash');
    setTimeout(function(){sel.classList.remove('wrong-flash','selected');item.classList.remove('wrong-flash');},700);
  }
});

function answerQuiz(btn,chosen,correct,qid){
  var q=document.getElementById(qid);if(!q)return;
  q.querySelectorAll('.quiz-opt').forEach(function(b,i){b.disabled=true;if(i===correct)b.classList.add('correct');else if(i===chosen&&chosen!==correct)b.classList.add('wrong');});
}

function ngCheck(uid,min,max,lang){
  var inp=document.getElementById('ngi'+uid);if(!inp)return;
  var guess=parseInt(inp.value);if(isNaN(guess))return;
  var secret=window['_ng_'+uid];
  var hist=document.getElementById('ngh'+uid);var msg=document.getElementById('ngm'+uid);
  var chip=document.createElement('span');
  chip.style.cssText='padding:3px 10px;border-radius:20px;font-size:13px;font-weight:700;border:1px solid;margin:2px;display:inline-block;';
  if(guess===secret){
    chip.textContent=guess+' ✓';chip.style.background='rgba(42,157,143,.1)';chip.style.borderColor='#2a9d8f';chip.style.color='#2a9d8f';
    if(hist)hist.appendChild(chip);
    if(msg)msg.innerHTML='<span style="color:#2a9d8f;font-size:18px">🎉 '+(lang==='ru'?'Правильно!':'To\\'g\\'ri! Topdingiz!')+'</span>';
    inp.disabled=true;
  }else{
    var dir=guess<secret?(lang==='ru'?'📈 Число больше!':'📈 Son kattaroq!'):(lang==='ru'?'📉 Число меньше!':'📉 Son kichikroq!');
    chip.textContent=guess+(guess<secret?' ↑':' ↓');chip.style.background=guess<secret?'rgba(59,130,246,.1)':'rgba(239,68,68,.08)';chip.style.borderColor=guess<secret?'#3b82f6':'#ef4444';chip.style.color=guess<secret?'#3b82f6':'#ef4444';
    if(hist)hist.appendChild(chip);
    if(msg){msg.textContent=dir;msg.style.color=guess<secret?'#3b82f6':'#ef4444';}
    inp.value='';inp.focus();
  }
}

function initAnagramInline(div){
  var words=JSON.parse(div.dataset.words||'[]');var lang=div.dataset.lang||'uz';
  var cur=0,score=0;
  function shuffle(s){var a=s.split('');for(var i=a.length-1;i>0;i--){var j=Math.floor(Math.random()*(i+1));var t=a[i];a[i]=a[j];a[j]=t;}return a.join('');}
  function renderQ(){
    if(cur>=words.length){div.innerHTML='<div style="text-align:center"><div style="font-size:36px;font-weight:900;color:#16a34a">'+Math.round(score/words.length*100)+'%</div><div>'+score+'/'+words.length+'</div></div>';return;}
    var w=words[cur];var sh=shuffle(w.word.toUpperCase());
    div.innerHTML='<div style="font-size:26px;font-weight:900;letter-spacing:5px;color:#16a34a;text-align:center;padding:12px;background:rgba(22,163,74,.08);border-radius:10px;margin-bottom:12px">'+sh+'</div>'+(w.hint?'<div style="font-size:13px;color:#6c757d;font-style:italic;margin-bottom:8px">💡 '+w.hint+'</div>':'')+'<input style="width:100%;padding:9px;border:2px solid #dee2e6;border-radius:8px;font-size:15px;text-transform:uppercase;outline:none;font-family:inherit;color:inherit;background:var(--card);margin-bottom:8px" placeholder="...">'+'<button class="check-btn" onclick="(function(b){var inp=b.previousElementSibling;var ok=inp.value.trim().toUpperCase()===\\''+w.word.toUpperCase()+'\\';if(ok)score++;inp.disabled=true;inp.style.borderColor=ok?\\'#2a9d8f\\':\\'#e63946\\';var fb=document.createElement(\\'div\\');fb.style.marginTop=\\'6px\\';fb.innerHTML=ok?\\'<span style=color:#2a9d8f;font-weight:700>✅ To\\\\x27g\\\\x27ri!</span>\\':\\'<span style=color:#e63946>❌ To\\\\x27g\\\\x27ri: <b>'+w.word+'</b></span>\\';b.parentElement.appendChild(fb);var nb=document.createElement(\\'button\\');nb.className=\\'check-btn\\';nb.style.marginLeft=\\'8px\\';nb.textContent=cur+1<words.length?\\'Keyingi →\\':\\'📊 Natija\\';nb.onclick=function(){cur++;renderQ();};b.parentElement.appendChild(nb);b.remove();})(this)">✅ Tekshirish</button>';
  }
  renderQ();
}

function initFlashInline(div){
  var cards=JSON.parse(div.dataset.cards||'[]').sort(function(){return Math.random()-.5;});
  var sec=parseInt(div.dataset.sec)||2;var cur=0,score=0;
  function next(){
    if(cur>=cards.length){div.innerHTML='<div style="text-align:center"><div style="font-size:36px;font-weight:900;color:#b45309">'+Math.round(score/cards.length*100)+'%</div><div>'+score+'/'+cards.length+'</div></div>';return;}
    var c=cards[cur];
    div.innerHTML='<div style="background:linear-gradient(135deg,#b45309,#d97706);color:#fff;border-radius:12px;padding:20px;font-size:22px;font-weight:800;text-align:center;margin-bottom:12px">'+c.front+'</div>'+'<div id="fc-cd" style="text-align:center;font-size:13px;color:#6c757d;margin-bottom:10px">⏱ '+sec+' soniya...</div>'+'<div id="fc-ans" style="display:none"><input style="width:100%;padding:9px;border:2px solid #dee2e6;border-radius:8px;font-size:14px;font-family:inherit;background:var(--card);color:inherit;margin-bottom:8px;outline:none" placeholder="Esladingizmi? Yozing..."><button class="check-btn" onclick="(function(b){var inp=b.previousElementSibling;var ok=inp.value.trim().toLowerCase()===\\''+c.back.toLowerCase()+'\\';if(ok)score++;inp.disabled=true;inp.style.borderColor=ok?\\'#2a9d8f\\':\\'#e63946\\';var fb=document.createElement(\\'div\\');fb.innerHTML=ok?\\'<span style=color:#2a9d8f;font-weight:700>✅</span>\\':\\'<span style=color:#e63946>❌ <b>'+c.back+'</b></span>\\';b.parentElement.appendChild(fb);var nb=document.createElement(\\'button\\');nb.className=\\'check-btn\\';nb.style.marginLeft=\\'8px\\';nb.textContent=cur+1<cards.length?\\'Keyingi →\\':\\'📊\\';nb.onclick=function(){cur++;next();};b.parentElement.appendChild(nb);b.remove();})(this)">✅ Tekshirish</button></div>';
    var left=sec;var cd=setInterval(function(){left--;var el=div.querySelector('#fc-cd');if(el)el.textContent='⏱ '+left+' soniya...';if(left<=0){clearInterval(cd);var card=div.querySelector('div:first-child');if(card){card.style.filter='blur(6px)';card.style.opacity='.3';}var ans=div.querySelector('#fc-ans');if(ans)ans.style.display='block';}},1000);
  }
  next();
}

function initMemGame(grid,pairs){
  var cards=[];
  pairs.forEach(function(p,i){cards.push({pairId:i,text:p.front});cards.push({pairId:i,text:p.back});});
  cards=cards.sort(function(){return Math.random()-.5;});
  var sel=null,matched=[],busy=false;
  function render(){
    grid.innerHTML='';
    cards.forEach(function(c,idx){
      var div=document.createElement('div');div.className='mg-card';
      var isOpen=sel&&sel.idx===idx;var isDone=matched.indexOf(c.pairId)>=0;
      if(isDone){div.classList.add('matched');div.textContent=c.text;}
      else if(isOpen){div.classList.add('open');div.textContent=c.text;}
      else{div.innerHTML='<span style="font-size:22px">🃏</span>';}
      if(!isDone&&!isOpen&&!busy){div.onclick=function(){
        if(busy)return;
        if(!sel){sel={idx:idx,card:c};render();return;}
        if(sel.card.pairId===c.pairId&&sel.idx!==idx){
          matched.push(c.pairId);sel=null;render();
        }else{
          busy=true;var prev=sel;sel=null;render();
          setTimeout(function(){busy=false;sel={idx:idx,card:c};render();},800);
        }
      };}
      grid.appendChild(div);
    });
  }
  render();
}

function startChain(btn,sec,uid){
  var div=btn.closest('.block-card');
  var chips=Array.from(div.querySelectorAll('.chain-chip')).map(function(c){return c.textContent;});
  var cc=div.querySelector('#cc'+uid);if(cc)cc.style.display='none';
  var idx=0;btn.disabled=true;btn.textContent='⏳';
  function show(){
    if(idx>=chips.length){btn.textContent='✅';var ca=div.querySelector('#ca'+uid);if(ca)ca.style.display='block';return;}
    btn.textContent='👁 '+chips[idx];idx++;setTimeout(show,sec*1000);
  }
  show();
}
function checkChain(btn){
  var div=btn.closest('.block-card');var inp=div.querySelector('.chain-inp');
  var chips=Array.from(div.querySelectorAll('.chain-chip')).map(function(c){return c.textContent;});
  var given=inp.value.split(',').map(function(s){return s.trim();});
  var correct=0;
  var fb=div.querySelector('.chain-fb');
  var rows=chips.map(function(c,i){var ok=given[i]&&given[i].toLowerCase()===c.toLowerCase();if(ok)correct++;return'<span style="padding:3px 8px;border-radius:6px;background:'+(ok?'rgba(42,157,143,.1)':'rgba(239,68,68,.08)')+';border:1px solid '+(ok?'#2a9d8f':'#ef4444')+';color:'+(ok?'#2a9d8f':'#ef4444')+';font-size:13px;margin:2px;display:inline-block">'+(i+1)+'. '+c+'</span>';}).join('');
  if(fb)fb.innerHTML=rows+'<div style="font-weight:700;margin-top:8px;color:#7c3aed">'+Math.round(correct/chips.length*100)+'%</div>';
  btn.disabled=true;inp.disabled=true;
}
"""

    first_id = lessons_data[0]['id'] if lessons_data else ''
    html = f"""<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(site_title)}</title>
<style>{css}</style>
</head>
<body>
<aside id="sidebar">
  <div id="sb-head">
    <button id="sb-toggle" title="Yig'ish / Ochish">✕</button>
    <span id="sb-title">📚 {esc(site_title)}</span>
  </div>
  <nav id="nav-list">{nav_items}</nav>
</aside>
<div id="main-content">
  {lessons_html}
</div>
<script>
{js}
</script>
</body>
</html>"""
    return html