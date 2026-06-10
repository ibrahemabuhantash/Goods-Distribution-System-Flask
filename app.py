from flask import Flask, render_template, request, redirect, session, flash, send_file
import sqlite3
import csv
import io
import os
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps


app = Flask(__name__)

app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
DB = os.environ.get("DB_NAME", "distribution.db")

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=3600
)

STATUSES = ["جديد", "قيد التجهيز", "قيد التوصيل", "مكتمل", "ملغي"]
SHIP = ["مجدولة", "خرجت للتوصيل", "تم التسليم", "فشل التسليم"]


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def column_exists(conn, table, column):
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(col["name"] == column for col in cols)


def init_db():
    c = db()
    cur = c.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT DEFAULT 'user'
    );

    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        sku TEXT,
        price REAL DEFAULT 0,
        quantity INTEGER DEFAULT 0,
        min_quantity INTEGER DEFAULT 5,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS customers(
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        phone TEXT,
        address TEXT,
        city TEXT,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS drivers(
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        phone TEXT,
        license_no TEXT,
        status TEXT DEFAULT 'متاح'
    );

    CREATE TABLE IF NOT EXISTS vehicles(
        id INTEGER PRIMARY KEY,
        plate_no TEXT NOT NULL,
        type TEXT,
        capacity REAL DEFAULT 0,
        status TEXT DEFAULT 'متاحة'
    );

    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY,
        customer_id INTEGER,
        status TEXT DEFAULT 'جديد',
        payment_status TEXT DEFAULT 'غير مدفوع',
        total REAL DEFAULT 0,
        notes TEXT,
        created_at TEXT,
        FOREIGN KEY(customer_id) REFERENCES customers(id)
    );

    CREATE TABLE IF NOT EXISTS order_items(
        id INTEGER PRIMARY KEY,
        order_id INTEGER,
        product_id INTEGER,
        quantity INTEGER DEFAULT 1,
        price REAL DEFAULT 0,
        subtotal REAL DEFAULT 0,
        FOREIGN KEY(order_id) REFERENCES orders(id),
        FOREIGN KEY(product_id) REFERENCES products(id)
    );

    CREATE TABLE IF NOT EXISTS shipments(
        id INTEGER PRIMARY KEY,
        order_id INTEGER,
        driver_id INTEGER,
        vehicle_id INTEGER,
        status TEXT DEFAULT 'مجدولة',
        delivery_date TEXT,
        delivered_at TEXT,
        notes TEXT,
        FOREIGN KEY(order_id) REFERENCES orders(id),
        FOREIGN KEY(driver_id) REFERENCES drivers(id),
        FOREIGN KEY(vehicle_id) REFERENCES vehicles(id)
    );

    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY,
        order_id INTEGER,
        amount REAL DEFAULT 0,
        method TEXT,
        paid_at TEXT,
        notes TEXT,
        FOREIGN KEY(order_id) REFERENCES orders(id)
    );
    """)

    # Migration لقاعدة البيانات القديمة
    if not column_exists(c, "orders", "payment_status"):
        cur.execute("ALTER TABLE orders ADD COLUMN payment_status TEXT DEFAULT 'غير مدفوع'")

    # Soft delete columns
    if not column_exists(c, "products", "is_active"):
        cur.execute("ALTER TABLE products ADD COLUMN is_active INTEGER DEFAULT 1")

    if not column_exists(c, "customers", "is_active"):
        cur.execute("ALTER TABLE customers ADD COLUMN is_active INTEGER DEFAULT 1")

    if not column_exists(c, "drivers", "is_active"):
        cur.execute("ALTER TABLE drivers ADD COLUMN is_active INTEGER DEFAULT 1")

    if not column_exists(c, "vehicles", "is_active"):
        cur.execute("ALTER TABLE vehicles ADD COLUMN is_active INTEGER DEFAULT 1")

    # إنشاء حساب المدير الافتراضي
    admin = cur.execute(
        "SELECT id FROM users WHERE username=?",
        ("admin",)
    ).fetchone()

    if not admin:
        cur.execute(
            "INSERT INTO users(username, password, role) VALUES(?,?,?)",
            ("admin", generate_password_hash("admin123"), "admin")
        )

    c.commit()
    c.close()


def auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "uid" not in session:
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "uid" not in session:
            return redirect("/login")

        if session.get("role") != "admin":
            return "غير مصرح", 403

        return f(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_user():
    return {
        "current_user": {
            "id": session.get("uid"),
            "username": session.get("username"),
            "role": session.get("role")
        }
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        c = db()
        u = c.execute(
            "SELECT * FROM users WHERE username=?",
            (username,)
        ).fetchone()
        c.close()

        if u and check_password_hash(u["password"], password):
            session.clear()
            session.permanent = True
            session["uid"] = u["id"]
            session["username"] = u["username"]
            session["role"] = u["role"]
            return redirect("/")

        flash("بيانات الدخول غير صحيحة", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
@auth
def home():
    c = db()

    stats = {
        "products": c.execute(
            "SELECT COUNT(*) FROM products WHERE is_active = 1"
        ).fetchone()[0],

        "customers": c.execute(
            "SELECT COUNT(*) FROM customers WHERE is_active = 1"
        ).fetchone()[0],

        "orders": c.execute(
            "SELECT COUNT(*) FROM orders"
        ).fetchone()[0],

        "shipments": c.execute(
            "SELECT COUNT(*) FROM shipments"
        ).fetchone()[0],

        "sales": c.execute(
            "SELECT COALESCE(SUM(total),0) FROM orders WHERE status!='ملغي'"
        ).fetchone()[0],

        "low": c.execute(
            "SELECT COUNT(*) FROM products WHERE quantity <= min_quantity AND is_active = 1"
        ).fetchone()[0],

        "paid": c.execute(
            "SELECT COALESCE(SUM(amount),0) FROM payments"
        ).fetchone()[0]
    }

    recent = c.execute("""
        SELECT o.*, c.name AS customer
        FROM orders o
        LEFT JOIN customers c ON c.id = o.customer_id
        ORDER BY o.id DESC
        LIMIT 5
    """).fetchall()

    c.close()

    return render_template("dashboard.html", stats=stats, recent=recent)


def page(table, title, cols, search_fields=None):
    allowed_tables = ["products", "customers", "drivers", "vehicles"]

    if table not in allowed_tables:
        return "Forbidden", 403

    q = request.args.get("q", "").strip()

    c = db()

    if q and search_fields:
        where = " OR ".join([f"{field} LIKE ?" for field in search_fields])
        params = [f"%{q}%"] * len(search_fields)

        rows = c.execute(
            f"""
            SELECT *
            FROM {table}
            WHERE is_active = 1 AND ({where})
            ORDER BY id DESC
            """,
            params
        ).fetchall()
    else:
        rows = c.execute(
            f"""
            SELECT *
            FROM {table}
            WHERE is_active = 1
            ORDER BY id DESC
            """
        ).fetchall()

    c.close()

    return render_template(
        "crud.html",
        table=table,
        title=title,
        columns=cols,
        rows=rows,
        q=q
    )


@app.route("/products")
@auth
def products():
    return page(
        "products",
        "المنتجات",
        [
            ("name", "اسم المنتج"),
            ("sku", "الكود"),
            ("price", "السعر"),
            ("quantity", "الكمية"),
            ("min_quantity", "حد التنبيه")
        ],
        ["name", "sku"]
    )


@app.route("/customers")
@auth
def customers():
    return page(
        "customers",
        "العملاء",
        [
            ("name", "الاسم"),
            ("phone", "الهاتف"),
            ("city", "المدينة"),
            ("address", "العنوان")
        ],
        ["name", "phone", "city", "address"]
    )


@app.route("/drivers")
@auth
def drivers():
    return page(
        "drivers",
        "السائقون",
        [
            ("name", "الاسم"),
            ("phone", "الهاتف"),
            ("license_no", "الرخصة"),
            ("status", "الحالة")
        ],
        ["name", "phone", "license_no", "status"]
    )


@app.route("/vehicles")
@auth
def vehicles():
    return page(
        "vehicles",
        "المركبات",
        [
            ("plate_no", "اللوحة"),
            ("type", "النوع"),
            ("capacity", "السعة"),
            ("status", "الحالة")
        ],
        ["plate_no", "type", "status"]
    )


@app.route("/<table>/add", methods=["POST"])
@auth
def add(table):
    allowed = {
        "products": ["name", "sku", "price", "quantity", "min_quantity"],
        "customers": ["name", "phone", "address", "city"],
        "drivers": ["name", "phone", "license_no", "status"],
        "vehicles": ["plate_no", "type", "capacity", "status"]
    }

    if table not in allowed:
        return "Forbidden", 403

    cols = allowed[table]
    vals = [request.form.get(x, "").strip() for x in cols]

    try:
        if table == "products":
            vals[2] = float(vals[2] or 0)
            vals[3] = int(vals[3] or 0)
            vals[4] = int(vals[4] or 5)

            if vals[2] < 0 or vals[3] < 0 or vals[4] < 0:
                flash("لا يمكن إدخال قيم سالبة", "danger")
                return redirect("/products")

        if table == "vehicles":
            vals[2] = float(vals[2] or 0)

            if vals[2] < 0:
                flash("السعة لا يمكن أن تكون سالبة", "danger")
                return redirect("/vehicles")

        if table in ["products", "customers"]:
            cols.append("created_at")
            vals.append(now())

        cols.append("is_active")
        vals.append(1)

        c = db()
        c.execute(
            f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join(['?'] * len(cols))})",
            vals
        )
        c.commit()
        c.close()

        flash("تمت الإضافة بنجاح", "success")

    except ValueError:
        flash("تأكد من إدخال الأرقام بشكل صحيح", "danger")

    except Exception:
        flash("حدث خطأ أثناء الإضافة", "danger")

    return redirect("/" + table)


@app.route("/<table>/edit/<int:id>", methods=["POST"])
@auth
def edit(table, id):
    allowed = {
        "products": ["name", "sku", "price", "quantity", "min_quantity"],
        "customers": ["name", "phone", "address", "city"],
        "drivers": ["name", "phone", "license_no", "status"],
        "vehicles": ["plate_no", "type", "capacity", "status"]
    }

    if table not in allowed:
        return "Forbidden", 403

    cols = allowed[table]
    vals = [request.form.get(x, "").strip() for x in cols]

    try:
        if table == "products":
            vals[2] = float(vals[2] or 0)
            vals[3] = int(vals[3] or 0)
            vals[4] = int(vals[4] or 5)

            if vals[2] < 0 or vals[3] < 0 or vals[4] < 0:
                flash("لا يمكن إدخال قيم سالبة", "danger")
                return redirect("/products")

        if table == "vehicles":
            vals[2] = float(vals[2] or 0)

            if vals[2] < 0:
                flash("السعة لا يمكن أن تكون سالبة", "danger")
                return redirect("/vehicles")

        set_clause = ", ".join([f"{col}=?" for col in cols])
        vals.append(id)

        c = db()
        c.execute(
            f"UPDATE {table} SET {set_clause} WHERE id=?",
            vals
        )
        c.commit()
        c.close()

        flash("تم التعديل بنجاح", "success")

    except ValueError:
        flash("تأكد من إدخال الأرقام بشكل صحيح", "danger")

    except Exception:
        flash("حدث خطأ أثناء التعديل", "danger")

    return redirect("/" + table)


# الحذف أصبح Soft Delete
@app.route("/<table>/delete/<int:id>")
@admin_required
def delete(table, id):
    if table not in ["products", "customers", "drivers", "vehicles"]:
        return "Forbidden", 403

    c = db()

    row = c.execute(
        f"SELECT id FROM {table} WHERE id=? AND is_active = 1",
        (id,)
    ).fetchone()

    if not row:
        c.close()
        flash("السجل غير موجود أو محذوف مسبقاً", "warning")
        return redirect("/" + table)

    c.execute(
        f"""
        UPDATE {table}
        SET is_active = 0
        WHERE id = ?
        """,
        (id,)
    )

    c.commit()
    c.close()

    flash("تم حذف السجل من العرض بنجاح", "success")
    return redirect("/" + table)


@app.route("/orders", methods=["GET", "POST"])
@auth
def orders():
    c = db()

    if request.method == "POST":
        try:
            customer_id = int(request.form.get("customer_id"))
            product_id = int(request.form.get("product_id"))
            quantity = int(request.form.get("quantity", 0))
            notes = request.form.get("notes", "").strip()

            if quantity <= 0:
                flash("الكمية يجب أن تكون أكبر من صفر", "danger")
                c.close()
                return redirect("/orders")

            customer = c.execute(
                "SELECT id FROM customers WHERE id=? AND is_active = 1",
                (customer_id,)
            ).fetchone()

            if not customer:
                flash("العميل غير موجود أو محذوف", "danger")
                c.close()
                return redirect("/orders")

            product = c.execute(
                "SELECT * FROM products WHERE id=? AND is_active = 1",
                (product_id,)
            ).fetchone()

            if not product:
                flash("المنتج غير موجود أو محذوف", "danger")
                c.close()
                return redirect("/orders")

            if product["quantity"] < quantity:
                flash("الكمية غير متوفرة", "danger")
                c.close()
                return redirect("/orders")

            total = quantity * product["price"]

            c.execute("BEGIN")

            cur = c.execute("""
                INSERT INTO orders(customer_id, status, payment_status, total, notes, created_at)
                VALUES(?,?,?,?,?,?)
            """, (
                customer_id,
                "جديد",
                "غير مدفوع",
                total,
                notes,
                now()
            ))

            order_id = cur.lastrowid

            c.execute("""
                INSERT INTO order_items(order_id, product_id, quantity, price, subtotal)
                VALUES(?,?,?,?,?)
            """, (
                order_id,
                product_id,
                quantity,
                product["price"],
                total
            ))

            c.execute("""
                UPDATE products
                SET quantity = quantity - ?
                WHERE id = ?
            """, (
                quantity,
                product_id
            ))

            c.commit()
            flash("تم إنشاء الطلب بنجاح", "success")

        except Exception:
            c.rollback()
            flash("حدث خطأ أثناء إنشاء الطلب", "danger")

    rows = c.execute("""
        SELECT 
            o.*,
            c.name AS customer,
            COALESCE(SUM(p.amount), 0) AS paid,
            o.total - COALESCE(SUM(p.amount), 0) AS remaining
        FROM orders o
        LEFT JOIN customers c ON c.id = o.customer_id
        LEFT JOIN payments p ON p.order_id = o.id
        GROUP BY o.id
        ORDER BY o.id DESC
    """).fetchall()

    customers = c.execute("""
        SELECT *
        FROM customers
        WHERE is_active = 1
        ORDER BY name
    """).fetchall()

    products = c.execute("""
        SELECT *
        FROM products
        WHERE quantity > 0 AND is_active = 1
        ORDER BY name
    """).fetchall()

    c.close()

    return render_template(
        "orders.html",
        rows=rows,
        customers=customers,
        products=products,
        statuses=STATUSES
    )


@app.route("/orders/status/<int:id>", methods=["POST"])
@auth
def order_status(id):
    new_status = request.form.get("status")

    if new_status not in STATUSES:
        flash("حالة الطلب غير صحيحة", "danger")
        return redirect("/orders")

    c = db()

    old_order = c.execute(
        "SELECT status FROM orders WHERE id=?",
        (id,)
    ).fetchone()

    if not old_order:
        c.close()
        flash("الطلب غير موجود", "danger")
        return redirect("/orders")

    old_status = old_order["status"]

    items = c.execute("""
        SELECT product_id, quantity
        FROM order_items
        WHERE order_id=?
    """, (id,)).fetchall()

    try:
        c.execute("BEGIN")

        if old_status != "ملغي" and new_status == "ملغي":
            for item in items:
                c.execute("""
                    UPDATE products
                    SET quantity = quantity + ?
                    WHERE id = ?
                """, (
                    item["quantity"],
                    item["product_id"]
                ))

        elif old_status == "ملغي" and new_status != "ملغي":
            for item in items:
                product = c.execute("""
                    SELECT quantity
                    FROM products
                    WHERE id=?
                """, (
                    item["product_id"],
                )).fetchone()

                if not product or product["quantity"] < item["quantity"]:
                    c.rollback()
                    c.close()
                    flash("لا يمكن إعادة تفعيل الطلب، الكمية غير متوفرة", "danger")
                    return redirect("/orders")

                c.execute("""
                    UPDATE products
                    SET quantity = quantity - ?
                    WHERE id = ?
                """, (
                    item["quantity"],
                    item["product_id"]
                ))

        c.execute("""
            UPDATE orders
            SET status=?
            WHERE id=?
        """, (
            new_status,
            id
        ))

        c.commit()
        flash("تم تحديث حالة الطلب", "success")

    except Exception:
        c.rollback()
        flash("حدث خطأ أثناء تحديث حالة الطلب", "danger")

    c.close()
    return redirect("/orders")


def update_payment_status(c, order_id):
    order = c.execute("""
        SELECT total
        FROM orders
        WHERE id=?
    """, (
        order_id,
    )).fetchone()

    if not order:
        return

    paid = c.execute("""
        SELECT COALESCE(SUM(amount),0) AS paid
        FROM payments
        WHERE order_id=?
    """, (
        order_id,
    )).fetchone()["paid"]

    total = order["total"]

    if paid <= 0:
        status = "غير مدفوع"
    elif paid < total:
        status = "مدفوع جزئياً"
    else:
        status = "مدفوع بالكامل"

    c.execute("""
        UPDATE orders
        SET payment_status=?
        WHERE id=?
    """, (
        status,
        order_id
    ))


@app.route("/shipments", methods=["GET", "POST"])
@auth
def shipments():
    c = db()

    if request.method == "POST":
        try:
            order_id = int(request.form.get("order_id"))
            driver_id = int(request.form.get("driver_id"))
            vehicle_id = int(request.form.get("vehicle_id"))
            delivery_date = request.form.get("delivery_date", "")
            notes = request.form.get("notes", "").strip()

            order = c.execute("""
                SELECT *
                FROM orders
                WHERE id=?
            """, (
                order_id,
            )).fetchone()

            if not order:
                flash("الطلب غير موجود", "danger")
                c.close()
                return redirect("/shipments")

            if order["status"] in ["مكتمل", "ملغي"]:
                flash("لا يمكن جدولة شحنة لطلب مكتمل أو ملغي", "danger")
                c.close()
                return redirect("/shipments")

            driver = c.execute(
                "SELECT * FROM drivers WHERE id=? AND is_active = 1",
                (driver_id,)
            ).fetchone()

            vehicle = c.execute(
                "SELECT * FROM vehicles WHERE id=? AND is_active = 1",
                (vehicle_id,)
            ).fetchone()

            if not driver or not vehicle:
                flash("السائق أو المركبة غير موجودة أو محذوفة", "danger")
                c.close()
                return redirect("/shipments")

            c.execute("BEGIN")

            c.execute("""
                INSERT INTO shipments(order_id, driver_id, vehicle_id, status, delivery_date, notes)
                VALUES(?,?,?,?,?,?)
            """, (
                order_id,
                driver_id,
                vehicle_id,
                "مجدولة",
                delivery_date,
                notes
            ))

            c.execute("""
                UPDATE orders
                SET status=?
                WHERE id=?
            """, (
                "قيد التوصيل",
                order_id
            ))

            c.execute("""
                UPDATE drivers
                SET status=?
                WHERE id=?
            """, (
                "مشغول",
                driver_id
            ))

            c.execute("""
                UPDATE vehicles
                SET status=?
                WHERE id=?
            """, (
                "مشغولة",
                vehicle_id
            ))

            c.commit()
            flash("تمت جدولة الشحنة بنجاح", "success")

        except Exception:
            c.rollback()
            flash("حدث خطأ أثناء جدولة الشحنة", "danger")

    rows = c.execute("""
        SELECT
            s.*,
            o.total,
            o.status AS order_status,
            c.name AS customer,
            d.name AS driver,
            v.plate_no AS vehicle
        FROM shipments s
        LEFT JOIN orders o ON o.id = s.order_id
        LEFT JOIN customers c ON c.id = o.customer_id
        LEFT JOIN drivers d ON d.id = s.driver_id
        LEFT JOIN vehicles v ON v.id = s.vehicle_id
        ORDER BY s.id DESC
    """).fetchall()

    orders_list = c.execute("""
        SELECT o.*, c.name AS customer
        FROM orders o
        LEFT JOIN customers c ON c.id = o.customer_id
        WHERE o.status NOT IN ('مكتمل', 'ملغي')
        ORDER BY o.id DESC
    """).fetchall()

    drivers_list = c.execute("""
        SELECT *
        FROM drivers
        WHERE is_active = 1
        ORDER BY name
    """).fetchall()

    vehicles_list = c.execute("""
        SELECT *
        FROM vehicles
        WHERE is_active = 1
        ORDER BY plate_no
    """).fetchall()

    c.close()

    return render_template(
        "shipments.html",
        rows=rows,
        orders=orders_list,
        drivers=drivers_list,
        vehicles=vehicles_list,
        statuses=SHIP
    )


@app.route("/shipments/status/<int:id>", methods=["POST"])
@auth
def shipment_status(id):
    new_status = request.form.get("status")

    if new_status not in SHIP:
        flash("حالة الشحنة غير صحيحة", "danger")
        return redirect("/shipments")

    c = db()

    shipment = c.execute("""
        SELECT *
        FROM shipments
        WHERE id=?
    """, (
        id,
    )).fetchone()

    if not shipment:
        c.close()
        flash("الشحنة غير موجودة", "danger")
        return redirect("/shipments")

    try:
        c.execute("BEGIN")

        delivered_at = None

        if new_status == "تم التسليم":
            delivered_at = now()

            c.execute("""
                UPDATE orders
                SET status=?
                WHERE id=?
            """, (
                "مكتمل",
                shipment["order_id"]
            ))

            c.execute("""
                UPDATE drivers
                SET status=?
                WHERE id=?
            """, (
                "متاح",
                shipment["driver_id"]
            ))

            c.execute("""
                UPDATE vehicles
                SET status=?
                WHERE id=?
            """, (
                "متاحة",
                shipment["vehicle_id"]
            ))

        elif new_status == "فشل التسليم":
            c.execute("""
                UPDATE orders
                SET status=?
                WHERE id=?
            """, (
                "قيد التوصيل",
                shipment["order_id"]
            ))

            c.execute("""
                UPDATE drivers
                SET status=?
                WHERE id=?
            """, (
                "متاح",
                shipment["driver_id"]
            ))

            c.execute("""
                UPDATE vehicles
                SET status=?
                WHERE id=?
            """, (
                "متاحة",
                shipment["vehicle_id"]
            ))

        elif new_status == "خرجت للتوصيل":
            c.execute("""
                UPDATE orders
                SET status=?
                WHERE id=?
            """, (
                "قيد التوصيل",
                shipment["order_id"]
            ))

        c.execute("""
            UPDATE shipments
            SET status=?,
                delivered_at=COALESCE(?, delivered_at)
            WHERE id=?
        """, (
            new_status,
            delivered_at,
            id
        ))

        c.commit()
        flash("تم تحديث حالة الشحنة", "success")

    except Exception:
        c.rollback()
        flash("حدث خطأ أثناء تحديث الشحنة", "danger")

    c.close()
    return redirect("/shipments")


@app.route("/payments", methods=["GET", "POST"])
@auth
def payments():
    c = db()

    if request.method == "POST":
        try:
            order_id = int(request.form.get("order_id"))
            amount = float(request.form.get("amount", 0))
            method = request.form.get("method", "").strip()
            notes = request.form.get("notes", "").strip()

            if amount <= 0:
                flash("المبلغ يجب أن يكون أكبر من صفر", "danger")
                c.close()
                return redirect("/payments")

            order = c.execute("""
                SELECT *
                FROM orders
                WHERE id=?
            """, (
                order_id,
            )).fetchone()

            if not order:
                flash("الطلب غير موجود", "danger")
                c.close()
                return redirect("/payments")

            if order["status"] == "ملغي":
                flash("لا يمكن تسجيل دفعة على طلب ملغي", "danger")
                c.close()
                return redirect("/payments")

            paid = c.execute("""
                SELECT COALESCE(SUM(amount),0) AS paid
                FROM payments
                WHERE order_id=?
            """, (
                order_id,
            )).fetchone()["paid"]

            remaining = order["total"] - paid

            if amount > remaining:
                flash("المبلغ أكبر من المتبقي على الطلب", "danger")
                c.close()
                return redirect("/payments")

            c.execute("BEGIN")

            c.execute("""
                INSERT INTO payments(order_id, amount, method, paid_at, notes)
                VALUES(?,?,?,?,?)
            """, (
                order_id,
                amount,
                method,
                now(),
                notes
            ))

            update_payment_status(c, order_id)

            c.commit()
            flash("تم تسجيل الدفعة بنجاح", "success")

        except ValueError:
            flash("تأكد من إدخال المبلغ بشكل صحيح", "danger")

        except Exception:
            c.rollback()
            flash("حدث خطأ أثناء تسجيل الدفعة", "danger")

    rows = c.execute("""
        SELECT
            p.*,
            c.name AS customer,
            o.total AS order_total,
            o.payment_status
        FROM payments p
        LEFT JOIN orders o ON o.id = p.order_id
        LEFT JOIN customers c ON c.id = o.customer_id
        ORDER BY p.id DESC
    """).fetchall()

    orders_list = c.execute("""
        SELECT
            o.*,
            c.name AS customer,
            COALESCE(SUM(p.amount),0) AS paid,
            o.total - COALESCE(SUM(p.amount),0) AS remaining
        FROM orders o
        LEFT JOIN customers c ON c.id = o.customer_id
        LEFT JOIN payments p ON p.order_id = o.id
        WHERE o.status != 'ملغي'
        GROUP BY o.id
        HAVING remaining > 0
        ORDER BY o.id DESC
    """).fetchall()

    c.close()

    return render_template(
        "payments.html",
        rows=rows,
        orders=orders_list
    )


@app.route("/reports")
@auth
def reports():
    c = db()

    low = c.execute("""
        SELECT *
        FROM products
        WHERE quantity <= min_quantity
          AND is_active = 1
        ORDER BY quantity ASC
    """).fetchall()

    sales = c.execute("""
        SELECT
            status,
            COUNT(*) AS count,
            COALESCE(SUM(total),0) AS total
        FROM orders
        GROUP BY status
    """).fetchall()

    top_products = c.execute("""
        SELECT
            p.name,
            SUM(oi.quantity) AS sold_qty,
            SUM(oi.subtotal) AS total_sales
        FROM order_items oi
        JOIN products p ON p.id = oi.product_id
        JOIN orders o ON o.id = oi.order_id
        WHERE o.status != 'ملغي'
        GROUP BY p.id
        ORDER BY sold_qty DESC
        LIMIT 10
    """).fetchall()

    payments_summary = c.execute("""
        SELECT
            COALESCE(SUM(o.total),0) AS total_orders,
            COALESCE((SELECT SUM(amount) FROM payments),0) AS total_paid,
            COALESCE(SUM(o.total),0) - COALESCE((SELECT SUM(amount) FROM payments),0) AS remaining
        FROM orders o
        WHERE o.status != 'ملغي'
    """).fetchone()

    late_shipments = c.execute("""
        SELECT
            s.*,
            c.name AS customer,
            d.name AS driver
        FROM shipments s
        LEFT JOIN orders o ON o.id = s.order_id
        LEFT JOIN customers c ON c.id = o.customer_id
        LEFT JOIN drivers d ON d.id = s.driver_id
        WHERE s.status NOT IN ('تم التسليم', 'فشل التسليم')
          AND s.delivery_date IS NOT NULL
          AND s.delivery_date < date('now')
        ORDER BY s.delivery_date ASC
    """).fetchall()

    c.close()

    return render_template(
        "reports.html",
        low=low,
        sales=sales,
        top_products=top_products,
        payments_summary=payments_summary,
        late_shipments=late_shipments
    )


@app.route("/export/<table>.csv")
@auth
def export_table(table):
    allowed = {
        "products": """
            SELECT id, name, sku, price, quantity, min_quantity, created_at
            FROM products
            WHERE is_active = 1
            ORDER BY id DESC
        """,
        "customers": """
            SELECT id, name, phone, city, address, created_at
            FROM customers
            WHERE is_active = 1
            ORDER BY id DESC
        """,
        "drivers": """
            SELECT id, name, phone, license_no, status
            FROM drivers
            WHERE is_active = 1
            ORDER BY id DESC
        """,
        "vehicles": """
            SELECT id, plate_no, type, capacity, status
            FROM vehicles
            WHERE is_active = 1
            ORDER BY id DESC
        """,
        "orders": """
            SELECT
                o.id,
                c.name AS customer,
                o.status,
                o.payment_status,
                o.total,
                COALESCE(SUM(p.amount),0) AS paid,
                o.total - COALESCE(SUM(p.amount),0) AS remaining,
                o.created_at
            FROM orders o
            LEFT JOIN customers c ON c.id = o.customer_id
            LEFT JOIN payments p ON p.order_id = o.id
            GROUP BY o.id
            ORDER BY o.id DESC
        """,
        "shipments": """
            SELECT
                s.id,
                s.order_id,
                c.name AS customer,
                d.name AS driver,
                v.plate_no AS vehicle,
                s.status,
                s.delivery_date,
                s.delivered_at,
                s.notes
            FROM shipments s
            LEFT JOIN orders o ON o.id = s.order_id
            LEFT JOIN customers c ON c.id = o.customer_id
            LEFT JOIN drivers d ON d.id = s.driver_id
            LEFT JOIN vehicles v ON v.id = s.vehicle_id
            ORDER BY s.id DESC
        """,
        "payments": """
            SELECT
                p.id,
                p.order_id,
                c.name AS customer,
                p.amount,
                p.method,
                p.paid_at,
                p.notes
            FROM payments p
            LEFT JOIN orders o ON o.id = p.order_id
            LEFT JOIN customers c ON c.id = o.customer_id
            ORDER BY p.id DESC
        """
    }

    if table not in allowed:
        return "Forbidden", 403

    c = db()
    rows = c.execute(allowed[table]).fetchall()
    c.close()

    out = io.StringIO()
    writer = csv.writer(out)

    if rows:
        writer.writerow(rows[0].keys())
        for r in rows:
            writer.writerow([r[k] for k in r.keys()])
    else:
        writer.writerow(["لا توجد بيانات"])

    return send_file(
        io.BytesIO(out.getvalue().encode("utf-8-sig")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"{table}.csv"
    )


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
