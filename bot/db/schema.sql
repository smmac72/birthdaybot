PRAGMA foreign_keys = ON;

-- canonical schema aligned with runtime repos

CREATE TABLE IF NOT EXISTS users (
  user_id      INTEGER PRIMARY KEY,
  username     TEXT,
  chat_id      INTEGER,
  birth_day    SMALLINT,
  birth_month  SMALLINT,
  birth_year   INTEGER,
  tz           INTEGER NOT NULL DEFAULT 0,     -- fixed hour offset
  alert_hours  INTEGER NOT NULL DEFAULT 0,     -- legacy
  alert_days   INTEGER NOT NULL DEFAULT 0,     -- new model (days + time)
  alert_time   TEXT DEFAULT '09:00',
  lang         TEXT DEFAULT 'ru',
  created_at   TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_chat ON users(chat_id);
CREATE INDEX IF NOT EXISTS idx_users_bday ON users(birth_month, birth_day);

-- friends as in repo_friends (supports unregistered usernames)
CREATE TABLE IF NOT EXISTS friends (
  owner_user_id    INTEGER NOT NULL,
  friend_user_id   INTEGER,
  friend_username  TEXT,
  birth_day        SMALLINT,
  birth_month      SMALLINT,
  birth_year       INTEGER,
  PRIMARY KEY(owner_user_id, friend_user_id, friend_username)
);

CREATE INDEX IF NOT EXISTS idx_f_owner ON friends(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_f_friend_id ON friends(friend_user_id);
CREATE INDEX IF NOT EXISTS idx_f_friend_un ON friends(LOWER(friend_username));

-- groups as in repo_groups
CREATE TABLE IF NOT EXISTS groups (
  group_id         TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  code             TEXT UNIQUE NOT NULL,
  creator_user_id  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_g_code ON groups(code);

CREATE TABLE IF NOT EXISTS group_members (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id         TEXT NOT NULL,
  member_user_id   INTEGER,
  member_username  TEXT,
  birth_day        SMALLINT,
  birth_month      SMALLINT,
  birth_year       INTEGER,
  joined_at        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_gm_group ON group_members(group_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gm_unique_uid_full ON group_members(group_id, member_user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gm_unique_uname_full ON group_members(group_id, member_username);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gm_unique_uid ON group_members(group_id, member_user_id) WHERE member_user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_gm_unique_uname ON group_members(group_id, member_username) WHERE member_username IS NOT NULL;

-- optional helper tables (kept for compatibility)
CREATE TABLE IF NOT EXISTS notifications_sent (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id    INTEGER NOT NULL,
  date_ymd     TEXT    NOT NULL,
  UNIQUE(person_id, date_ymd)
);
