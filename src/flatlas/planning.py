"""Immutable dry-run duplicate plan creation and queries."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from flatlas.db import now_ns
from flatlas.queries import duplicate_groups


def create_dry_run_plan(connection: sqlite3.Connection, root_id: int) -> str:
    groups = duplicate_groups(connection, root_id=root_id)
    plan_id = str(uuid.uuid4())
    source = connection.execute("SELECT id FROM scan WHERE root_id=? ORDER BY id DESC LIMIT 1", (root_id,)).fetchone()
    policy = {"version": 1, "canonical_selection": "lexical_path", "execution": "dry_run_only"}
    timestamp = now_ns()
    with connection:
        connection.execute(
            "INSERT INTO plan(id, root_id, source_scan_id, status, policy_json, created_at_ns, immutable_at_ns) VALUES (?, ?, ?, 'dry_run', ?, ?, ?)",
            (plan_id, root_id, source["id"] if source else None, json.dumps(policy, sort_keys=True), timestamp, timestamp),
        )
        ordinal = 0
        for group in groups:
            ordered = sorted(group["paths"], key=lambda item: item["path_display"])
            canonical = ordered[0]
            canonical_inode = (canonical["device"], canonical["inode"])
            for replacement in ordered[1:]:
                if replacement["inode"] is not None and (replacement["device"], replacement["inode"]) == canonical_inode:
                    continue  # already a hardlink of the canonical file
                ordinal += 1
                preconditions = {
                    "same_registered_filesystem": True,
                    "expected_canonical_path": canonical["path_display"],
                    "expected_replacement_path": replacement["path_display"],
                    "execution_not_implemented": True,
                }
                connection.execute(
                    """INSERT INTO plan_operation(plan_id, ordinal, action, canonical_path_id, replacement_path_id,
                    expected_size, expected_full_digest, expected_algorithm, preconditions_json, status)
                    VALUES (?, ?, 'hardlink', ?, ?, ?, ?, ?, ?, 'planned')""",
                    (plan_id, ordinal, canonical["id"], replacement["id"], group["logical_size"], bytes.fromhex(group["digest"]), group["algorithm"], json.dumps(preconditions, sort_keys=True)),
                )
    return plan_id


def plan_operations(connection: sqlite3.Connection, plan_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT 'operation' AS type, po.ordinal, po.action, c.path_display AS canonical_path, r.path_display AS replacement_path,
        po.expected_size, hex(po.expected_full_digest) AS expected_digest, po.expected_algorithm,
        po.preconditions_json, po.status FROM plan_operation po
        JOIN path_with_display c ON c.id=po.canonical_path_id JOIN path_with_display r ON r.id=po.replacement_path_id
        WHERE po.plan_id=? ORDER BY po.ordinal""", (plan_id,)
    ).fetchall()
    return [dict(row) for row in rows]


def list_plans(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return plan summaries without exposing their operation details."""
    rows = connection.execute(
        """SELECT 'plan' AS type, plan.id, root.root_path_display AS root, plan.source_scan_id, plan.status,
        plan.created_at_ns, plan.immutable_at_ns, plan.exported_at_ns, plan.note,
        COUNT(po.id) AS operation_count,
        COALESCE(SUM(po.expected_size), 0) AS theoretical_savings
        FROM plan
        JOIN root ON root.id=plan.root_id
        LEFT JOIN plan_operation po ON po.plan_id=plan.id
        GROUP BY plan.id
        ORDER BY plan.created_at_ns DESC, plan.id DESC"""
    ).fetchall()
    return [dict(row) for row in rows]
