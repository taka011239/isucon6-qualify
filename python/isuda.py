from flask import Flask, request, jsonify, abort, render_template, redirect, session, url_for, g
import MySQLdb.cursors
import html
import json
import math
import os
import pathlib
import random
import re
import string
import urllib
import redis
from pydarts import PyDarts

regex_br = re.compile("\n")

static_folder = pathlib.Path(__file__).resolve().parent.parent / 'public'
app = Flask(__name__, static_folder = str(static_folder), static_url_path='')

app.secret_key = 'tonymoris'

_config = {
    'db_host':       os.environ.get('ISUDA_DB_HOST', 'localhost'),
    'db_port':       int(os.environ.get('ISUDA_DB_PORT', '3306')),
    'db_user':       os.environ.get('ISUDA_DB_USER', 'root'),
    'db_password':   os.environ.get('ISUDA_DB_PASSWORD', ''),
    'isupam_origin': os.environ.get('ISUPAM_ORIGIN', 'http://localhost:5050'),
}

def config(key):
    if key in _config:
        return _config[key]
    else:
        raise "config value of %s undefined" % key

def dbh():
    if hasattr(g, 'db'):
        return g.db
    else:
        g.db = MySQLdb.connect(**{
            'host': config('db_host'),
            'port': config('db_port'),
            'user': config('db_user'),
            'passwd': config('db_password'),
            'db': 'isuda',
            'charset': 'utf8mb4',
            'cursorclass': MySQLdb.cursors.DictCursor,
            'autocommit': True,
        })
        cur = g.db.cursor()
        cur.execute("SET SESSION sql_mode='TRADITIONAL,NO_AUTO_VALUE_ON_ZERO,ONLY_FULL_GROUP_BY'")
        cur.execute('SET NAMES utf8mb4')
        return g.db


def redish():
    if hasattr(g, 'redis'):
        return g.redis
    else:
        g.redis = redis.Redis()
        return g.redis

@app.teardown_request
def close_db(exception=None):
    if hasattr(request, 'db'):
        request.db.close()

@app.template_filter()
def ucfirst(str):
    return str[0].upper() + str[-len(str) + 1:]

def set_name(func):
    import functools
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if "user_id" in session:
            request.user_id   = user_id = session['user_id']
            client = redish()
            password = client.get('user:%s' % user_id)

            if not password:
                abort(403)
            request.user_name = user_id

        return func(*args, **kwargs)
    return wrapper

def authenticate(func):
    import functools
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not hasattr(request, 'user_id'):
            abort(403)
        return func(*args, **kwargs)

    return wrapper

@app.route('/initialize')
def get_initialize():
    cur = dbh().cursor()
    cur.execute('DELETE FROM entry WHERE id > 7101')
    cur.execute('TRUNCATE star')

    cur.execute('SELECT keyword FROM entry WHERE id <= 7101')
    client = redish()
    client.flushall()
    keywords = {keyword_replacement(e['keyword']): len(e['keyword'])
                for e in cur.fetchall()}
    client.zadd('entry:keyword:length', **keywords)

    cur.execute('SELECT name from user')
    for u in cur.fetchall():
        client.set('user:%s' % u['name'], u['name'])

    if hasattr(g, 'da'):
        delattr(g, 'da')

    if hasattr(g, 'entry_html'):
        delattr(g, 'entry_html')

    client.delete('htmlify')

    return jsonify(result = 'ok')

def keyword_replacement(keyword):
    url = url_for('get_keyword', keyword=keyword)
    link = "<a href=\"%s\">%s</a>" % (url, html.escape(keyword))
    return '{}\t{}'.format(html.escape(keyword), link)

@app.route('/')
@set_name
def get_index():
    PER_PAGE = 10
    page = int(request.args.get('page', '1'))

    cur = dbh().cursor()
    cur.execute('SELECT * FROM entry ORDER BY updated_at DESC LIMIT %s OFFSET %s', (PER_PAGE, PER_PAGE * (page - 1),))
    entries = cur.fetchall()
    for entry in entries:
        entry['html'] = htmlify(entry['keyword'], entry['description'])
        entry['stars'] = load_stars(entry['keyword'], cur)

    cur.execute('SELECT COUNT(*) AS count FROM entry')
    row = cur.fetchone()
    total_entries = row['count']
    last_page = int(math.ceil(total_entries / PER_PAGE))
    pages = range(max(1, page - 5), min(last_page, page+5) + 1)

    return render_template('index.html', entries = entries, page = page, last_page = last_page, pages = pages)

@app.route('/robots.txt')
def get_robot_txt():
    abort(404)

@app.route('/keyword', methods=['POST'])
@set_name
@authenticate
def create_keyword():
    keyword = request.form['keyword']
    if keyword == None or len(keyword) == 0:
        abort(400)

    user_id = request.user_id
    description = request.form['description']

    if is_spam_contents(description) or is_spam_contents(keyword):
        abort(400)

    cur = dbh().cursor()
    sql = """
        INSERT INTO entry (author_id, keyword, description, created_at, updated_at)
        VALUES (%s,%s,%s,NOW(), NOW())
        ON DUPLICATE KEY UPDATE
        author_id = %s, keyword = %s, description = %s, updated_at = NOW()
"""
    client = redish()
    client.zadd('entry:keyword:length', keyword_replacement(keyword), len(keyword))

    if hasattr(g, 'da'):
        delattr(g, 'da')

    if hasattr(g, 'entry_html'):
        delattr(g, 'entry_html')

    client.delete('htmlify')

    cur.execute(sql, (user_id, keyword, description, user_id, keyword, description))
    return redirect('/')

@app.route('/register')
@set_name
def get_register():
    return render_template('authenticate.html', action = 'register')

@app.route('/register', methods=['POST'])
def post_register():
    name = request.form['name']
    pw   = request.form['password']
    if name == None or name == '' or pw == None or pw == '':
        abort(400)

    user_id = register(name, pw)
    session['user_id'] = user_id
    return redirect('/')

def register(user, password):
    client = redish()
    client.set('user:%s' % user, password)
    return user

def random_string(n):
    return ''.join([random.choice(string.ascii_letters + string.digits) for i in range(n)])

@app.route('/login')
@set_name
def get_login():
    return render_template('authenticate.html', action = 'login')

@app.route('/login', methods=['POST'])
def post_login():
    name = request.form['name']
    client = redish()
    password = client.get('user:%s' % name)
    if password == None || password.decode('utf-8') != request.form['password']:
        abort(403)

    session['user_id'] = name
    return redirect('/')

@app.route('/logout')
def get_logout():
    session.pop('user_id', None)
    return redirect('/')

@app.route('/keyword/<keyword>')
@set_name
def get_keyword(keyword):
    if keyword == '':
        abort(400)

    cur = dbh().cursor()
    cur.execute('SELECT * FROM entry WHERE keyword = %s', (keyword,))
    entry = cur.fetchone()
    if entry == None:
        abort(404)

    entry['html'] = htmlify(entry['keyword'], entry['description'])
    entry['stars'] = load_stars(entry['keyword'], cur)
    return render_template('keyword.html', entry = entry)

@app.route('/keyword/<keyword>', methods=['POST'])
@set_name
@authenticate
def delete_keyword(keyword):
    if keyword == '':
        abort(400)

    cur = dbh().cursor()
    cur.execute('SELECT * FROM entry WHERE keyword = %s', (keyword, ))
    row = cur.fetchone()
    if row == None:
        abort(404)

    cur.execute('DELETE FROM entry WHERE keyword = %s', (keyword,))
    client = redish()
    client.zrem('entry:keyword:length', keyword)

    if hasattr(g, 'entry_html'):
        delattr(g, 'entry_html')

    client.delete('htmlify')

    return redirect('/')

@app.route("/stars")
def get_stars():
    cur = dbh().cursor()
    return jsonify(stars = load_stars(request.args['keyword'], cur))

@app.route("/stars", methods=['POST'])
def post_stars():
    keyword = request.args.get('keyword', "")
    if keyword == None or keyword == "":
        keyword = request.form['keyword']

    cur = dbh().cursor()
    cur.execute('SELECT COUNT(1) AS cnt FROM entry WHERE keyword = %s', (keyword,))
    cnt = cur.fetchone()
    if not cnt['cnt']:
        abort(404)

    cur.execute('INSERT INTO star (keyword, user_name, created_at) VALUES (%s, %s, NOW())', (keyword, request.args.get('user', '', )))

    return jsonify(result = 'ok')

def htmlify(keyword, content):
    client = redish()
    cache = client.hget('htmlify', keyword)
    if cache:
        return cache.decode('utf-8')

    if not hasattr(g, 'entry_html'):
        print('miss hit 1')
        g.entry_html = {}
    cache = g.entry_html.get(keyword)
    if cache:
        return cache
    print('miss hit 2')

    if content == None or content == '':
        return ''

    keywords = client.zrevrange('entry:keyword:length', 0, -1)
    keywords = [tuple(k.decode('utf-8').split('\t')) for k in keywords] # [(keyword, link), ...]
    kw2link = {k: l for k, l in keywords}

    result = html.escape(content)

    if not hasattr(g, 'da'):
        PyDarts.build([k for k,_ in keywords], '/tmp/isuda.da')
        g.da = PyDarts('/tmp/isuda.da')

    keywords = [k for k,_ in g.da.match(result)]

    def replace_keyword(m):
        return kw2link[m.group(0)]

    regex_keyword = re.compile("(%s)" % '|'.join([re.escape(k) for k in keywords]))
    result = re.sub(regex_keyword, replace_keyword, result)

    result = re.sub(regex_br, "<br />", result)
    g.entry_html[keyword] = result
    client.hset('htmlify', keyword, result)
    return result

def load_stars(keyword, cur):
    cur.execute('SELECT * FROM star WHERE keyword = %s', (keyword, ))
    return cur.fetchall()

def is_spam_contents(content):
    with urllib.request.urlopen(config('isupam_origin'), urllib.parse.urlencode({ "content": content }).encode('utf-8')) as res:
        data = json.loads(res.read().decode('utf-8'))
        return not data['valid']

    return False

if __name__ == "__main__":
    app.run()


