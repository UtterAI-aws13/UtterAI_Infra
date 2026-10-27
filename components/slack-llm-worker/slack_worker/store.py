"""CAS store contract shared by SQLite offline experiments and DynamoDB."""
import copy
import json
import sqlite3
import threading


class Conflict(Exception):
    pass


class Store:
    def mutate(self, key, fn):
        for _ in range(20):
            old = self.get(key)
            new = fn(copy.deepcopy(old))
            if new is None:
                return old
            rev = old["rev"] if old else 0
            new["rev"] = rev + 1
            try:
                self.cas(key, rev, new)
                return new
            except Conflict:
                continue
        raise Conflict("contention limit")


class SQLiteStore(Store):
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, rev INTEGER, body TEXT)")
        self.db.commit()

    def get(self, key):
        with self.lock:
            row = self.db.execute("SELECT body FROM state WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def cas(self, key, rev, value):
        with self.lock, self.db:
            if rev == 0:
                try:
                    self.db.execute("INSERT INTO state VALUES (?,?,?)", (key, value["rev"], json.dumps(value)))
                except sqlite3.IntegrityError as e:
                    raise Conflict() from e
            else:
                cur = self.db.execute("UPDATE state SET rev=?,body=? WHERE k=? AND rev=?",
                                      (value["rev"], json.dumps(value), key, rev))
                if cur.rowcount != 1:
                    raise Conflict()


class DynamoStore(Store):
    def __init__(self, client, table_name):
        # Use the low-level client: boto3 Resource instances must not be shared across worker threads.
        self.client, self.table_name = client, table_name

    def get(self, key):
        item = self.client.get_item(TableName=self.table_name, Key={"pk": {"S": key}},
                                    ConsistentRead=True).get("Item")
        return json.loads(item["body"]["S"]) if item else None

    def cas(self, key, rev, value):
        args = {"TableName": self.table_name,
                "Item": {"pk": {"S": key}, "rev": {"N": str(value["rev"])},
                         "body": {"S": json.dumps(value)}, "expires": {"N": str(int(value["expires"]))}},
                "ConditionExpression": "attribute_not_exists(pk)" if rev == 0 else "rev = :r"}
        if rev:
            args["ExpressionAttributeValues"] = {":r": {"N": str(rev)}}
        try:
            self.client.put_item(**args)
        except Exception as e:
            if getattr(e, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise Conflict() from e
            raise
