import sqlite3

import pytest

from app.database import execute_readonly, explain_query_plan
from app.sql_analysis import analyze_performance
from app.sql_validator import validate_sql


def test_catalog_introspection(catalog):
    assert catalog.table_names() == ["Departments", "Employees", "Customers", "Products", "Orders", "OrderItems"]
    customers = catalog.table("customers")
    assert customers.column("state").sample_values[:2] == ["AZ", "CA"]
    assert "California" in customers.column("State").description
    assert catalog.has_fk("Orders", "CustomerID", "Customers", "CustomerID")
    assert catalog.has_fk("Customers", "CustomerID", "Orders", "CustomerID")
    assert catalog.has_fk("Employees", "ManagerID", "Employees", "EmployeeID")
    assert not catalog.has_fk("Customers", "CustomerID", "Orders", "OrderID")


def test_prompt_text_contains_annotations(catalog):
    text = catalog.prompt_text()
    assert "CREATE TABLE Orders" in text
    assert "REFERENCES Customers(CustomerID)" in text
    assert "'Delivered'" in text  # low-cardinality values are listed
    assert "range: " in text      # date ranges are listed


def test_relevant_tables(catalog):
    assert catalog.relevant_tables("show employees and their department") == ["Departments", "Employees"]
    assert set(catalog.relevant_tables("revenue by product from order items")) >= {"Products", "OrderItems", "Orders"}
    assert catalog.relevant_tables("hello there") == catalog.table_names()  # nothing matched: everything


@pytest.mark.parametrize("sql", [
    "DELETE FROM Orders",
    "UPDATE Orders SET Status = 'x'",
    "DROP TABLE Orders",
    "INSERT INTO Departments (Name, Location, Budget) VALUES ('x', 'y', 1)",
    "PRAGMA writable_schema = ON",
    "ATTACH DATABASE ':memory:' AS m",
])
def test_sandbox_blocks_writes_even_without_validation(sql, db_path):
    with pytest.raises(sqlite3.DatabaseError):
        execute_readonly(db_path, sql, 10, 5)
    assert execute_readonly(db_path, "SELECT COUNT(*) FROM Orders", 10, 5).rows == [[1500]]


def test_row_cap_and_truncation(db_path):
    result = execute_readonly(db_path, "SELECT OrderID FROM Orders", 25, 5)
    assert result.row_count == 25 and result.truncated
    assert result.columns == ["OrderID"]


def test_timeout(db_path):
    runaway = ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) "
               "SELECT COUNT(*) FROM n")
    with pytest.raises(TimeoutError):
        execute_readonly(db_path, runaway, 10, 0.2)


def test_explain_query_plan(db_path):
    plan = explain_query_plan(db_path, "SELECT * FROM Customers WHERE State = 'CA'")
    assert any("SCAN" in row["detail"] for row in plan)


def analyze(sql, catalog, db_path):
    v = validate_sql(sql, catalog, db_path)
    assert v.ok, v.errors
    return analyze_performance(v.sql, catalog, v.plan)


def test_index_suggested_for_filtered_full_scan(catalog, db_path):
    report = analyze("SELECT FirstName FROM Customers WHERE State = 'CA'", catalog, db_path)
    assert [s.ddl for s in report.index_suggestions] == ["CREATE INDEX idx_customers_state ON Customers(State);"]
    assert report.cost_level == "low"


def test_no_index_suggested_when_already_indexed(catalog, db_path):
    report = analyze("SELECT OrderID FROM Orders WHERE CustomerID = 5", catalog, db_path)
    assert report.index_suggestions == []
    assert any("idx_orders_customer" in line for line in report.plan)


def test_equality_columns_come_before_range(catalog, db_path):
    report = analyze("SELECT OrderID FROM Orders WHERE OrderDate >= '2025-01-01' AND Status = 'Shipped'", catalog, db_path)
    assert report.index_suggestions[0].columns == ["Status", "OrderDate"]


def test_lint_notes(catalog, db_path):
    report = analyze("SELECT c.CustomerID, (SELECT COUNT(*) FROM Orders o WHERE o.CustomerID = c.CustomerID) AS n "
                     "FROM Customers c WHERE c.Email LIKE '%gmail%' AND strftime('%Y', c.CreatedAt) = '2024'",
                     catalog, db_path)
    notes = " ".join(report.notes)
    assert "wildcard" in notes and "correlated subquery" in notes and "function of CreatedAt" in notes


def test_unused_join_detection(catalog, db_path):
    report = analyze("SELECT o.OrderID FROM Orders o LEFT JOIN Customers c ON c.CustomerID = o.CustomerID",
                     catalog, db_path)
    assert report.unused_joins and "can be removed" in report.unused_joins[0]
    used = analyze("SELECT o.OrderID, c.State FROM Orders o JOIN Customers c ON c.CustomerID = o.CustomerID",
                   catalog, db_path)
    assert used.unused_joins == []
