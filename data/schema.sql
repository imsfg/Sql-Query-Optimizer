-- Sample schema for the SQL Assistant: a small sales company.
-- Dialect: SQLite. Dates are stored as ISO-8601 text ('YYYY-MM-DD').
--
-- Some filter columns (Customers.State, Employees.HireDate, Orders.OrderDate,
-- Orders.Status) are intentionally left unindexed so the optimizer has
-- something to recommend.

CREATE TABLE Departments (
    DepartmentID   INTEGER PRIMARY KEY,
    Name           TEXT    NOT NULL UNIQUE,
    Location       TEXT    NOT NULL,
    Budget         REAL    NOT NULL
);

CREATE TABLE Employees (
    EmployeeID     INTEGER PRIMARY KEY,
    FirstName      TEXT    NOT NULL,
    LastName       TEXT    NOT NULL,
    Email          TEXT    NOT NULL UNIQUE,
    HireDate       TEXT    NOT NULL,
    JobTitle       TEXT    NOT NULL,
    Salary         REAL    NOT NULL,
    DepartmentID   INTEGER NOT NULL REFERENCES Departments(DepartmentID),
    ManagerID      INTEGER REFERENCES Employees(EmployeeID)
);

CREATE TABLE Customers (
    CustomerID     INTEGER PRIMARY KEY,
    FirstName      TEXT    NOT NULL,
    LastName       TEXT    NOT NULL,
    Email          TEXT    NOT NULL UNIQUE,
    Phone          TEXT,
    City           TEXT    NOT NULL,
    State          TEXT    NOT NULL,
    Country        TEXT    NOT NULL DEFAULT 'USA',
    CreatedAt      TEXT    NOT NULL
);

CREATE TABLE Products (
    ProductID      INTEGER PRIMARY KEY,
    Name           TEXT    NOT NULL,
    Category       TEXT    NOT NULL,
    UnitPrice      REAL    NOT NULL,
    StockQuantity  INTEGER NOT NULL
);

CREATE TABLE Orders (
    OrderID        INTEGER PRIMARY KEY,
    CustomerID     INTEGER NOT NULL REFERENCES Customers(CustomerID),
    EmployeeID     INTEGER NOT NULL REFERENCES Employees(EmployeeID),
    OrderDate      TEXT    NOT NULL,
    Status         TEXT    NOT NULL,
    TotalAmount    REAL    NOT NULL
);

CREATE TABLE OrderItems (
    OrderItemID    INTEGER PRIMARY KEY,
    OrderID        INTEGER NOT NULL REFERENCES Orders(OrderID),
    ProductID      INTEGER NOT NULL REFERENCES Products(ProductID),
    Quantity       INTEGER NOT NULL,
    UnitPrice      REAL    NOT NULL
);

CREATE INDEX idx_orders_customer ON Orders(CustomerID);
CREATE INDEX idx_orderitems_order ON OrderItems(OrderID);
CREATE INDEX idx_employees_department ON Employees(DepartmentID);
