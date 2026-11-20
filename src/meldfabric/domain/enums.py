"""Enums shared by configuration and runtime layers."""

from enum import StrEnum


class ManagedBy(StrEnum):
    CONFIG = "config"
    CODE = "code"
    REMOTE = "remote"


class DatabaseEngine(StrEnum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    CUSTOM = "custom"


class SessionPolicy(StrEnum):
    PER_CALL = "per_call"
    PARENT_THREAD = "parent_thread"
    PER_USER = "per_user"


class InteractionPolicy(StrEnum):
    FAIL = "fail"
    BUBBLE_TO_PARENT = "bubble_to_parent"
    PARENT_AUTO_ANSWER = "parent_auto_answer"
    DIRECT_INTERACTION = "direct_interaction"
