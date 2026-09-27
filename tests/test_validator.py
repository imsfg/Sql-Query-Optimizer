import pytest

from app.sql_validator import clean_sql, transpile, validate_sql


def check(sql, catalog, db_path):
    return validate_sql(sql, catalog, db_path)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM Employees WHERE HireDate >= '2024-01-01'",
    "SELECT e.FirstName, d.Name FROM Employees e JOIN Departments d ON e.DepartmentID = d.DepartmentID",
    "SELECT e.FirstName, m.FirstName AS Manager FROM Employees e LEFT JOIN Employees m ON e.ManagerID = m.EmployeeID",
    "WITH t AS (SELECT CustomerID, SUM(TotalAmount) AS total FROM Orders GROUP BY CustomerID) "
    "SELECT c.FirstName, t.total FROM t JOIN Customers c ON c.CustomerID = t.CustomerID ORDER BY t.total DESC LIMIT 5",
    "SELECT c.FirstName FROM Customers c WHERE EXISTS "
    "(SELECT 1 FROM Orders o WHERE o.CustomerID = c.CustomerID AND o.Status = 'Shipped')",
    "SELECT State, COUNT(*) AS n FROM Customers GROUP BY State ORDER BY n DESC",
    "SELECT CustomerID FROM Orders WHERE CustomerID NOT IN (SELECT CustomerID FROM Customers WHERE State = 'CA')",
    "SELECT Category FROM Products UNION SELECT Name FROM Departments",
    "```sql\nSELECT Name FROM Products;\n```",
])
def test_valid_queries(sql, catalog, db_path):
    result = check(sql, catalog, db_path)
    assert result.ok, result.errors
    assert result.read_only
    assert result.plan  # engine accepted it


@pytest.mark.parametrize("sql, keyword", [
    ("DELETE FROM Orders", "DELETE"),
    ("UPDATE Employees SET Salary = 0", "UPDATE"),
    ("INSERT INTO Departments (Name, Location, Budget) VALUES ('x', 'y', 1)", "INSERT"),
    ("DROP TABLE Orders", "DROP"),
    ("ALTER TABLE Orders ADD COLUMN x INT", "ALTER"),
    ("TRUNCATE TABLE Orders", "TRUNCATE"),
    ("WITH d AS (DELETE FROM Orders RETURNING *) SELECT * FROM d", "DELETE"),
    ("SELECT * INTO Backup FROM Orders", "SELECT INTO"),
    ("PRAGMA table_info(Orders)", "PRAGMA"),
    ("ATTACH DATABASE 'other.db' AS other", "ATTACH"),
    ("SELECT load_extension('evil')", "LOAD_EXTENSION"),
])
def test_write_operations_rejected(sql, keyword, catalog, db_path):
    result = check(sql, catalog, db_path)
    assert not result.ok
    assert not result.read_only
    assert keyword in " ".join(result.errors)


def test_multiple_statements_rejected(catalog, db_path):
    result = check("SELECT 1; DROP TABLE Orders", catalog, db_path)
    assert not result.ok
    assert any("one SQL statement" in e for e in result.errors)


def test_unknown_table_with_suggestion(catalog, db_path):
    result = check("SELECT * FROM Employee", catalog, db_path)
    assert not result.ok
    assert "Unknown table 'Employee'" in result.errors[0] and "Did you mean 'Employees'" in result.errors[0]


@pytest.mark.parametrize("sql", ["SELECT Salry FROM Employees", "SELECT e.Salry FROM Employees e"])
def test_unknown_column_with_suggestion(sql, catalog, db_path):
    result = check(sql, catalog, db_path)
    assert not result.ok
    assert "Unknown column 'Salry'" in result.errors[0] and "Salary" in result.errors[0]


def test_unknown_column_inside_subquery(catalog, db_path):
    result = check("SELECT CustomerID FROM Orders WHERE CustomerID IN (SELECT CustomerID FROM Customers WHERE Stat = 'CA')",
                   catalog, db_path)
    assert not result.ok and "Stat" in result.errors[0]


def test_unknown_alias(catalog, db_path):
    result = check("SELECT x.FirstName FROM Employees e", catalog, db_path)
    assert not result.ok and "alias 'x'" in result.errors[0]


def test_join_on_unrelated_keys_is_rejected(catalog, db_path):
    result = check("SELECT c.FirstName FROM Customers c JOIN Orders o ON c.CustomerID = o.OrderID", catalog, db_path)
    assert not result.ok
    assert "does not match a declared relationship" in result.errors[0]
    assert "Orders.CustomerID -> Customers.CustomerID" in result.errors[0]


def test_join_on_non_key_columns_is_a_warning(catalog, db_path):
    result = check("SELECT c.FirstName FROM Customers c JOIN Employees e ON c.FirstName = e.FirstName", catalog, db_path)
    assert result.ok
    assert any("not a declared foreign key" in w for w in result.warnings)


def test_engine_catches_dialect_errors(catalog, db_path):
    result = check("SELECT YEAR(HireDate) FROM Employees", catalog, db_path)
    assert not result.ok and "no such function: YEAR" in result.errors[0]


def test_engine_catches_ambiguous_columns(catalog, db_path):
    result = check("SELECT DepartmentID FROM Employees e JOIN Departments d ON e.DepartmentID = d.DepartmentID",
                   catalog, db_path)
    assert not result.ok and "ambiguous" in result.errors[0]


def test_syntax_error(catalog, db_path):
    result = check("SELEC * FROM Orders", catalog, db_path)
    assert not result.ok and result.errors[0].startswith("Syntax error")


def test_select_star_warning(catalog, db_path):
    assert any("SELECT *" in w for w in check("SELECT * FROM Orders", catalog, db_path).warnings)
    assert not check("SELECT COUNT(*) FROM Orders", catalog, db_path).warnings


def test_clean_sql_and_transpile():
    assert clean_sql("```sql\nSELECT 1;\n```") == "SELECT 1"
    assert "EXTRACT" in transpile("SELECT strftime('%Y', OrderDate) FROM Orders", "postgres").upper() or \
        "TO_CHAR" in transpile("SELECT strftime('%Y', OrderDate) FROM Orders", "postgres").upper()
