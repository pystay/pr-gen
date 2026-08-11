-- pr-gen 商业化服务 Supabase (PostgreSQL) 建表脚本（开源免费版）
-- 与 server/app/db.py 的 SQLite schema 同构；部署 Supabase 后在 SQL Editor 执行。
-- 注意：本版本无任何支付/定价数据表（项目已完全免费开源）。

create table if not exists events (
    event_id    text primary key,
    event_type  text not null,
    payload     jsonb not null default '{}'::jsonb,
    received_at timestamptz not null default now()
);

-- 开源免费版用户（邮箱 sha256+pepper 哈希作查询键 + Fernet 加密存储原文）
create table if not exists users (
    id         bigserial primary key,
    email_hash text not null unique,
    email_enc  text not null,
    created_at timestamptz not null default now()
);

-- 免费订阅（全部功能开放，恒为 free/active）
create table if not exists subscriptions (
    id         bigserial primary key,
    account_id text not null unique,
    plan       text not null default 'free',
    status     text not null default 'active',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists usage (
    id         bigserial primary key,
    account_id text not null,
    plan       text not null default 'free',
    day        date not null,
    calls      integer not null default 0,
    cost       numeric(12,4) not null default 0,
    unique (account_id, day)
);

create table if not exists notifications (
    id         bigserial primary key,
    channel    text not null,             -- telegram | log
    message    text not null,
    created_at timestamptz not null default now()
);
