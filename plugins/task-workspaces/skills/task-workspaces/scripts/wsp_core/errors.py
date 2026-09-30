"""Stable error codes for the wsp CLI.

Agents and scripts rely on the code and exit status, never on message text.
"""

EXIT_CODES = {
    "INTERNAL": 1,
    "USAGE": 2,
    "CONFIG_MISSING": 10,
    "CONFIG_INVALID": 11,
    "REPO_UNKNOWN": 12,
    "SOURCE_INVALID": 13,
    "ORIGIN_MISMATCH": 14,
    "TASK_UNKNOWN": 15,
    "TREE_UNKNOWN": 16,
    "INVALID_ID": 17,
    "PATH_UNSAFE": 18,
    "PATH_CONFLICT": 19,
    "FETCH_FAILED": 20,
    "BRANCH_EXISTS": 21,
    "BRANCH_BUSY": 22,
    "GIT_FAILED": 23,
    "CONFLICTED_INDEX": 24,
    "LOCKED": 25,
    "DISK_LOW": 30,
    "QUOTA_EXCEEDED": 31,
    "LIMIT_REACHED": 32,
    "LEASE_HELD": 40,
    "PROCESS_RUNNING": 41,
    "PINNED": 42,
    "UNKNOWN_OWNERSHIP": 43,
    "UNIQUE_STATE": 44,
    "UNCLASSIFIED_IGNORED": 45,
    "CHECKPOINT_MISSING": 50,
    "CHECKPOINT_INVALID": 51,
    "RESTORE_CONFLICT": 52,
    "SOURCE_MISSING": 53,
    "DEPS_INCOMPATIBLE": 60,
    "DEPS_FAILED": 61,
    "PERMISSION": 70,
    "UNSUPPORTED": 71,
    "SPARSE_PROFILE_UNKNOWN": 80,
}


class WspError(Exception):
    def __init__(self, code, message, **details):
        if code not in EXIT_CODES:
            raise ValueError(f"unknown error code {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    @property
    def exit_code(self):
        return EXIT_CODES[self.code]

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}
