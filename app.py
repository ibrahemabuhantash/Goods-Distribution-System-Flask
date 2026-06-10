from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
import sqlite3, csv, io
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
app=Flask(__name__); app.secret_key='change-me'; DB='distribution.db'
STATUSES=['جديد','قيد التجهيز','قيد التوصيل','مكتمل','ملغي']; SHIP=['مجدولة','خرجت للتوصيل','تم التسليم','فشل التسليم']
def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
def init_db():
    c=db(); cur=c.cursor(); cur.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,role TEXT);
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY,name TEXT,sku TEXT,price REAL,quantity INTEGER,min_quantity INTEGER DEFAULT 5,created_at TEXT);
CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY,name TEXT,phone TEXT,address TEXT,city TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS drivers(id INTEGER PRIMARY KEY,name TEXT,phone TEXT,license_no TEXT,status TEXT DEFAULT 'متاح');
CREATE TABLE IF NOT EXISTS vehicles(id INTEGER PRIMARY KEY,plate_no TEXT,type TEXT,capacity REAL,status TEXT DEFAULT 'متاحة');
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY,customer_id INTEGER,status TEXT DEFAULT 'جديد',total REAL DEFAULT 0,notes TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS order_items(id INTEGER PRIMARY KEY,order_id INTEGER,product_id INTEGER,quantity INTEGER,price REAL,subtotal REAL);
CREATE TABLE IF NOT EXISTS shipments(id INTEGER PRIMARY KEY,order_id INTEGER,driver_id INTEGER,vehicle_id INTEGER,status TEXT,delivery_date TEXT,delivered_at TEXT,notes TEXT);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY,order_id INTEGER,amount REAL,method TEXT,paid_at TEXT,notes TEXT);
""")
    if not cur.execute('select id from users where username=?',('admin',)).fetchone(): cur.execute('insert into users(username,password,role) values(?,?,?)',('admin',generate_password_hash('admin123'),'admin'))
    c.commit(); c.close()
def auth(f):
    @wraps(f)
    def x(*a,**k):
        if 'uid' not in session: return redirect('/login')
        return f(*a,**k)
    return x
@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        c=db(); u=c.execute('select * from users where username=?',(request.form['username'],)).fetchone(); c.close()
        if u and check_password_hash(u['password'],request.form['password']): session['uid']=u['id']; return redirect('/')
        flash('بيانات الدخول غير صحيحة','danger')
    return render_template('login.html')
@app.route('/logout')
def logout(): session.clear(); return redirect('/login')
@app.route('/')
@auth
def home():
    c=db(); stats={k:c.execute(q).fetchone()[0] for k,q in {'products':'select count(*) from products','customers':'select count(*) from customers','orders':'select count(*) from orders','shipments':'select count(*) from shipments','sales':'select coalesce(sum(total),0) from orders where status!="ملغي"','low':'select count(*) from products where quantity<=min_quantity'}.items()}; recent=c.execute('select o.*,c.name customer from orders o left join customers c on c.id=o.customer_id order by o.id desc limit 5').fetchall(); c.close(); return render_template('dashboard.html',stats=stats,recent=recent)
def page(table,title,cols):
    c=db(); rows=c.execute(f'select * from {table} order by id desc').fetchall(); c.close(); return render_template('crud.html',table=table,title=title,columns=cols,rows=rows)
@app.route('/products')
@auth
def products(): return page('products','المنتجات',[('name','اسم المنتج'),('sku','الكود'),('price','السعر'),('quantity','الكمية'),('min_quantity','حد التنبيه')])
@app.route('/customers')
@auth
def customers(): return page('customers','العملاء',[('name','الاسم'),('phone','الهاتف'),('city','المدينة'),('address','العنوان')])
@app.route('/drivers')
@auth
def drivers(): return page('drivers','السائقون',[('name','الاسم'),('phone','الهاتف'),('license_no','الرخصة'),('status','الحالة')])
@app.route('/vehicles')
@auth
def vehicles(): return page('vehicles','المركبات',[('plate_no','اللوحة'),('type','النوع'),('capacity','السعة'),('status','الحالة')])
@app.route('/<table>/add',methods=['POST'])
@auth
def add(table):
    allowed={'products':['name','sku','price','quantity','min_quantity'],'customers':['name','phone','address','city'],'drivers':['name','phone','license_no','status'],'vehicles':['plate_no','type','capacity','status']}
    if table not in allowed: return 'Forbidden',403
    cols=allowed[table]; vals=[request.form.get(x,'') for x in cols]
    if table in ['products','customers']: cols.append('created_at'); vals.append(datetime.now().strftime('%Y-%m-%d %H:%M'))
    c=db(); c.execute(f"insert into {table}({','.join(cols)}) values({','.join(['?']*len(cols))})",vals); c.commit(); c.close(); return redirect('/'+table)
@app.route('/<table>/delete/<int:id>')
@auth
def delete(table,id):
    if table not in ['products','customers','drivers','vehicles']: return 'Forbidden',403
    c=db(); c.execute(f'delete from {table} where id=?',(id,)); c.commit(); c.close(); return redirect('/'+table)
@app.route('/orders',methods=['GET','POST'])
@auth
def orders():
    c=db()
    if request.method=='POST':
        p=c.execute('select * from products where id=?',(request.form['product_id'],)).fetchone(); q=int(request.form['quantity'])
        if not p or p['quantity']<q: flash('الكمية غير متوفرة','danger'); return redirect('/orders')
        total=q*p['price']; now=datetime.now().strftime('%Y-%m-%d %H:%M'); cur=c.execute('insert into orders(customer_id,total,notes,created_at) values(?,?,?,?)',(request.form['customer_id'],total,request.form.get('notes',''),now)); oid=cur.lastrowid
        c.execute('insert into order_items(order_id,product_id,quantity,price,subtotal) values(?,?,?,?,?)',(oid,p['id'],q,p['price'],total)); c.execute('update products set quantity=quantity-? where id=?',(q,p['id'])); c.commit(); flash('تم إنشاء الطلب','success')
    data=(c.execute('select o.*,c.name customer from orders o left join customers c on c.id=o.customer_id order by o.id desc').fetchall(), c.execute('select * from customers').fetchall(), c.execute('select * from products where quantity>0').fetchall()); c.close(); return render_template('orders.html',rows=data[0],customers=data[1],products=data[2],statuses=STATUSES)
@app.route('/orders/status/<int:id>', methods=['POST'])
@auth
def order_status(id):
    c = db()
    new_status = request.form['status']

    old_order = c.execute(
        'SELECT status FROM orders WHERE id=?',
        (id,)
    ).fetchone()

    # إذا تم إلغاء الطلب لأول مرة، رجّع الكمية للمخزون
    if old_order and old_order['status'] != 'ملغي' and new_status == 'ملغي':
        items = c.execute(
            'SELECT product_id, quantity FROM order_items WHERE order_id=?',
            (id,)
        ).fetchall()

        for item in items:
            c.execute(
                'UPDATE products SET quantity = quantity + ? WHERE id=?',
                (item['quantity'], item['product_id'])
            )

    c.execute(
        'UPDATE orders SET status=? WHERE id=?',
        (new_status, id)
    )

    c.commit()
    c.close()

    return redirect('/orders')

@app.route('/shipments',methods=['GET','POST'])
@auth
def shipments():
    c=db()
    if request.method=='POST': c.execute('insert into shipments(order_id,driver_id,vehicle_id,status,delivery_date,notes) values(?,?,?,?,?,?)',(request.form['order_id'],request.form['driver_id'],request.form['vehicle_id'],'مجدولة',request.form['delivery_date'],request.form.get('notes',''))); c.execute('update orders set status=? where id=?',('قيد التوصيل',request.form['order_id'])); c.commit(); flash('تمت جدولة الشحنة','success')
    rows=c.execute('select s.*,o.total,c.name customer,d.name driver,v.plate_no vehicle from shipments s left join orders o on o.id=s.order_id left join customers c on c.id=o.customer_id left join drivers d on d.id=s.driver_id left join vehicles v on v.id=s.vehicle_id order by s.id desc').fetchall(); orders=c.execute('select o.*,c.name customer from orders o left join customers c on c.id=o.customer_id where o.status!="مكتمل"').fetchall(); drivers=c.execute('select * from drivers').fetchall(); vehicles=c.execute('select * from vehicles').fetchall(); c.close(); return render_template('shipments.html',rows=rows,orders=orders,drivers=drivers,vehicles=vehicles,statuses=SHIP)
@app.route('/payments',methods=['GET','POST'])
@auth
def payments():
    c=db()
    if request.method=='POST': c.execute('insert into payments(order_id,amount,method,paid_at,notes) values(?,?,?,?,?)',(request.form['order_id'],request.form['amount'],request.form['method'],datetime.now().strftime('%Y-%m-%d %H:%M'),request.form.get('notes',''))); c.commit(); flash('تم تسجيل الدفعة','success')
    rows=c.execute('select p.*,c.name customer from payments p left join orders o on o.id=p.order_id left join customers c on c.id=o.customer_id order by p.id desc').fetchall(); orders=c.execute('select o.*,c.name customer from orders o left join customers c on c.id=o.customer_id').fetchall(); c.close(); return render_template('payments.html',rows=rows,orders=orders)
@app.route('/reports')
@auth
def reports():
    c=db(); low=c.execute('select * from products where quantity<=min_quantity').fetchall(); sales=c.execute('select status,count(*) count,coalesce(sum(total),0) total from orders group by status').fetchall(); c.close(); return render_template('reports.html',low=low,sales=sales)
@app.route('/export/orders.csv')
@auth
def export():
    c=db(); rows=c.execute('select o.id,c.name customer,o.status,o.total,o.created_at from orders o left join customers c on c.id=o.customer_id').fetchall(); c.close(); out=io.StringIO(); wr=csv.writer(out); wr.writerow(['ID','Customer','Status','Total','Date']); [wr.writerow([r['id'],r['customer'],r['status'],r['total'],r['created_at']]) for r in rows]; return send_file(io.BytesIO(out.getvalue().encode('utf-8-sig')),mimetype='text/csv',as_attachment=True,download_name='orders.csv')
if __name__=='__main__': init_db(); app.run(debug=True)
