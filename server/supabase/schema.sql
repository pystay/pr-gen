-- pr-gen 商业化服务 Supabase (PostgreSQL) 建表脚本
-- 与 server/app/db.py 的 SQLite schema 同构；部署 Supabase 后在 SQL Editor 执行。
-- 说明：events 表用于 Webhook 幂等；billing_model 预留按量付费扩展。

create table if not exists events (
    event_id    text primary key,
    event_type  text not null,
    payload     jsonb not null default '{}'::jsonb,
    received_at timestamptz not null default now()
);

create table if not exists subscriptions (
    id             bigserial primary key,
    account_id     text not null unique,
    account_type   text not null default 'user',
    account_login  text not null default '',
    plan           text not null default 'free',      -- free | pro | team
    status         text not null default 'active',    -- active | cancelled | expired
    seats          integer not null default 1,
    billing_model  text not null default 'per_seat',  -- 预留：pay_as_you_go
    effective_date timestamptz not null,
    expiry_date    timestamptz,
    price_locked   boolean not null default false,    -- 锁定老价格（自营订阅）
    locked_price   numeric(10,2),
    payment_channel text not null default '',         -- alipay | wechat | paypal | github
    billing_cycle  text not null default 'monthly',   -- monthly | yearly
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

-- 自营订阅用户（邮箱 sha256 哈希作查询键 + Fernet 加密存储原文）
create table if not exists users (
    id         bigserial primary key,
    email_hash text not null unique,
    email_enc  text not null,
    created_at timestamptz not null default now()
);

-- 支付流水（order_no 唯一，raw_callback 留痕用于对账；日志保留 ≥90 天）
create table if not exists payment_logs (
    id           bigserial primary key,
    order_no     text not null unique,
    user_id      bigint not null,
    tier         text not null,
    cycle        text not null,
    amount       numeric(12,2) not null,
    currency     text not null default 'CNY',
    channel      text not null,                        -- alipay | wechat | paypal
    status       text not null default 'pending',      -- pending | success | failed
    raw_callback jsonb not null default '{}'::jsonb,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create table if not exists orders (
    id         bigserial primary key,
    event_id   text not null,
    account_id text not null,
    plan       text not null,
    seats      integer not null,
    amount     numeric(12,2) not null,
    currency   text not null default 'USD',
    created_at timestamptz not null default now()
);

create table if not exists leads (
    id                bigserial primary key,
    company_enc       text not null,
    contact_email_enc text not null,
    dev_count         integer not null,
    environment       text not null,      -- aws | private_idc | hybrid
    needs_custom      boolean not null default false,
    special_notes_enc text not null default '',
    quote_amount      numeric(12,2),
    quote_currency    text not null default 'USD',
    quote_pdf_path    text not null default '',
    status            text not null default '待联系',  -- 待联系|已报价|已转化|已流失
    created_at        timestamptz not null default now()
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
    channel    text not null,             -- telegram | email | log
    message    text not null,
    created_at timestamptz not null default now()
);
