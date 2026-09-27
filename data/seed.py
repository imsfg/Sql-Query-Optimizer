"""Build the sample SQLite database from schema.sql with deterministic fake data.

Usage:  python -m data.seed [--db path] [--force]
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = DATA_DIR / "schema.sql"
DEFAULT_DB_PATH = DATA_DIR / "company.db"

FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda",
    "David", "Elizabeth", "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica",
    "Thomas", "Sarah", "Carlos", "Karen", "Daniel", "Lisa", "Wei", "Nancy", "Priya",
    "Betty", "Ahmed", "Sandra", "Kenji", "Ashley", "Diego", "Emily", "Omar", "Olivia",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Chen", "Patel", "Nguyen",
    "Kim", "Khan", "Tanaka", "Silva",
]
CITIES = [
    ("Los Angeles", "CA"), ("San Francisco", "CA"), ("San Diego", "CA"), ("Sacramento", "CA"),
    ("New York", "NY"), ("Buffalo", "NY"), ("Austin", "TX"), ("Houston", "TX"), ("Dallas", "TX"),
    ("Seattle", "WA"), ("Spokane", "WA"), ("Chicago", "IL"), ("Miami", "FL"), ("Orlando", "FL"),
    ("Boston", "MA"), ("Denver", "CO"), ("Phoenix", "AZ"), ("Portland", "OR"), ("Atlanta", "GA"),
]
DEPARTMENTS = [
    ("Sales", "New York", 1_200_000), ("Engineering", "San Francisco", 2_500_000),
    ("Marketing", "Chicago", 800_000), ("Finance", "New York", 600_000),
    ("Human Resources", "Austin", 400_000), ("Customer Support", "Denver", 500_000),
]
TITLES = {
    "Sales": ["Sales Representative", "Senior Sales Representative", "Account Executive"],
    "Engineering": ["Software Engineer", "Senior Software Engineer", "QA Engineer"],
    "Marketing": ["Marketing Specialist", "Content Strategist"],
    "Finance": ["Financial Analyst", "Accountant"],
    "Human Resources": ["HR Generalist", "Recruiter"],
    "Customer Support": ["Support Specialist", "Support Engineer"],
}
PRODUCTS = {
    "Electronics": [("Wireless Mouse", 24.99), ("Mechanical Keyboard", 89.99), ("27in Monitor", 279.0),
                    ("USB-C Hub", 39.5), ("Noise-Cancelling Headphones", 199.0), ("Webcam HD", 59.99),
                    ("Portable SSD 1TB", 109.0), ("Smart Speaker", 49.0)],
    "Furniture": [("Standing Desk", 499.0), ("Ergonomic Chair", 329.0), ("Bookshelf", 129.0),
                  ("Desk Lamp", 34.99), ("Filing Cabinet", 159.0), ("Monitor Arm", 79.0)],
    "Office Supplies": [("Notebook Pack", 12.99), ("Gel Pens (12)", 9.49), ("Stapler", 14.0),
                        ("Printer Paper (5 reams)", 42.0), ("Whiteboard", 69.0), ("Sticky Notes", 6.99),
                        ("Desk Organizer", 21.5)],
    "Software": [("Antivirus 1yr", 39.99), ("Office Suite License", 149.0), ("Photo Editor", 79.0),
                 ("Cloud Backup 1yr", 59.0), ("VPN 1yr", 49.99)],
    "Accessories": [("Laptop Sleeve", 29.0), ("Phone Stand", 15.99), ("Cable Organizer", 11.0),
                    ("Mouse Pad XL", 19.99), ("Laptop Stand", 45.0), ("Screen Cleaner", 8.49)],
}
ORDER_STATUSES = ["Delivered"] * 6 + ["Shipped"] * 2 + ["Processing", "Pending", "Cancelled"]


def _rand_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def build_database(db_path: Path = DEFAULT_DB_PATH, force: bool = False) -> Path:
    db_path = Path(db_path)
    if db_path.exists():
        if not force:
            return db_path
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    rng = random.Random(42)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text())

        conn.executemany(
            "INSERT INTO Departments (DepartmentID, Name, Location, Budget) VALUES (?, ?, ?, ?)",
            [(i + 1, n, loc, b) for i, (n, loc, b) in enumerate(DEPARTMENTS)],
        )

        # Employees: one head per department (no manager), then staff reporting to them.
        employees = []
        used_emails: set[str] = set()

        def unique_email(first: str, last: str, domain: str) -> str:
            base = f"{first}.{last}".lower()
            email, n = f"{base}@{domain}", 2
            while email in used_emails:
                email, n = f"{base}{n}@{domain}", n + 1
            used_emails.add(email)
            return email

        emp_id = 0
        heads = {}
        for dept_id, (dept_name, _, _) in enumerate(DEPARTMENTS, start=1):
            emp_id += 1
            first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
            hire = _rand_date(rng, date(2016, 1, 1), date(2020, 12, 31))
            employees.append((emp_id, first, last, unique_email(first, last, "company.com"), hire.isoformat(),
                              f"Head of {dept_name}", round(rng.uniform(150_000, 220_000), -2), dept_id, None))
            heads[dept_id] = emp_id
        for _ in range(34):
            emp_id += 1
            dept_id = rng.choices(range(1, 7), weights=[10, 8, 4, 3, 2, 4])[0]
            dept_name = DEPARTMENTS[dept_id - 1][0]
            first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
            hire = _rand_date(rng, date(2019, 1, 1), date(2025, 9, 30))
            employees.append((emp_id, first, last, unique_email(first, last, "company.com"), hire.isoformat(),
                              rng.choice(TITLES[dept_name]), round(rng.uniform(55_000, 145_000), -2),
                              dept_id, heads[dept_id]))
        conn.executemany("INSERT INTO Employees VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", employees)
        sales_reps = [e[0] for e in employees if e[7] == 1]

        customers = []
        for cust_id in range(1, 251):
            first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
            city, state = rng.choice(CITIES)
            phone = None if rng.random() < 0.1 else f"({rng.randint(200, 989)}) 555-{rng.randint(1000, 9999)}"
            created = _rand_date(rng, date(2021, 1, 1), date(2025, 6, 30))
            customers.append((cust_id, first, last, unique_email(first, last, "example.com"), phone, city, state,
                              "USA", created.isoformat()))
        conn.executemany("INSERT INTO Customers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", customers)

        products = []
        pid = 0
        for category, items in PRODUCTS.items():
            for name, price in items:
                pid += 1
                stock = 0 if pid % 9 == 0 else rng.randint(5, 400)  # a few items are out of stock
                products.append((pid, name, category, price, stock))
        conn.executemany("INSERT INTO Products VALUES (?, ?, ?, ?, ?)", products)

        orders, items = [], []
        item_id = 0
        for order_id in range(1, 1501):
            cust = rng.choice(customers)
            order_date = _rand_date(rng, date.fromisoformat(cust[8]), date(2025, 9, 30))
            lines = []
            for prod in rng.sample(products, rng.randint(1, 4)):
                item_id += 1
                qty = rng.randint(1, 5)
                lines.append((item_id, order_id, prod[0], qty, prod[3]))
            total = round(sum(q * p for _, _, _, q, p in lines), 2)
            orders.append((order_id, cust[0], rng.choice(sales_reps), order_date.isoformat(),
                           rng.choice(ORDER_STATUSES), total))
            items.extend(lines)
        conn.executemany("INSERT INTO Orders VALUES (?, ?, ?, ?, ?, ?)", orders)
        conn.executemany("INSERT INTO OrderItems VALUES (?, ?, ?, ?, ?)", items)
        conn.commit()
    finally:
        conn.close()
    return db_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--force", action="store_true", help="Rebuild even if the file exists")
    args = parser.parse_args()
    path = build_database(args.db, force=args.force)
    print(f"Database ready at {path}")
