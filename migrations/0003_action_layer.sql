-- 0003: the kinetic layer.
--
-- Agents never write to orders. They call kb.propose_action(action, params).
-- The database validates the params against the action's JSON schema and the
-- applicable risk limits, records every check, and approves or rejects in the
-- same transaction. Only an executor identity can claim approved requests and
-- record what it did. Raising a limit or releasing a kill switch is human-only.

create table kb.agent (
    id         text primary key,               -- 'hermes', 'gnoesis', 'risk_officer', ...
    name       text not null,
    role       text not null check (role in ('agent', 'risk_officer', 'executor', 'human')),
    db_role    text not null unique,           -- the Postgres login this agent connects as
    active     boolean not null default true,
    created_at timestamptz not null default now()
);

-- Limit history. The current limit for a scope is its latest row.
create table kb.risk_limit (
    id                 bigint generated always as identity primary key,
    scope              text not null check (scope in ('global', 'league', 'market', 'agent')),
    scope_key          text not null default '*',
    max_stake          numeric check (max_stake is null or max_stake >= 0),
    max_daily_exposure numeric check (max_daily_exposure is null or max_daily_exposure >= 0),
    max_price_age_s    integer check (max_price_age_s is null or max_price_age_s > 0),
    kill_switch        boolean not null default false,
    set_by             text not null,
    set_at             timestamptz not null default now(),
    reason             text,
    check (scope <> 'global' or scope_key = '*')
);
create trigger risk_limit_append_only before update or delete on kb.risk_limit
for each row execute function kb.forbid_change();

create view kb.risk_limit_current as
select distinct on (scope, scope_key) *
from kb.risk_limit
order by scope, scope_key, set_at desc, id desc;

create table kb.action_type (
    name          text primary key,
    description   text not null,
    param_schema  jsonb not null,
    allowed_roles text[] not null,
    executes      boolean not null    -- true: an executor acts on approved requests
);

insert into kb.action_type (name, description, allowed_roles, executes, param_schema) values
('propose_edge', 'Record a model price against a market. Informational; places nothing.',
 array['agent', 'risk_officer'], false,
 '{"type": "object", "additionalProperties": false,
   "required": ["market_id", "side", "model_prob", "source", "confidence"],
   "properties": {
     "market_id":  {"type": "integer", "minimum": 1},
     "side":       {"enum": ["home", "away", "over", "under"]},
     "model_prob": {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
     "source":     {"type": "string", "minLength": 1, "maxLength": 200},
     "confidence": {"type": "number", "minimum": 0, "maximum": 1},
     "rationale":  {"type": "string", "maxLength": 4000}}}'),
('place_order', 'Ask for a pregame order. Checked against stake, daily exposure, kill switch and the current price.',
 array['agent'], true,
 '{"type": "object", "additionalProperties": false,
   "required": ["market_id", "side", "stake", "limit_american"],
   "properties": {
     "market_id":      {"type": "integer", "minimum": 1},
     "side":           {"enum": ["home", "away", "over", "under"]},
     "stake":          {"type": "number", "exclusiveMinimum": 0},
     "limit_american": {"type": "integer", "not": {"minimum": -99, "maximum": 99}},
     "line":           {"type": "number"},
     "rationale":      {"type": "string", "maxLength": 4000}}}'),
('cancel_order', 'Ask to cancel an order the agent owns.',
 array['agent', 'risk_officer'], true,
 '{"type": "object", "additionalProperties": false,
   "required": ["order_id"],
   "properties": {"order_id": {"type": "integer", "minimum": 1},
                  "rationale": {"type": "string", "maxLength": 4000}}}'),
('set_risk_limit', 'Tighten a limit or engage a kill switch. Can never loosen.',
 array['agent', 'risk_officer'], false,
 '{"type": "object", "additionalProperties": false,
   "required": ["scope"],
   "properties": {
     "scope":              {"enum": ["global", "league", "market", "agent"]},
     "scope_key":          {"type": "string", "minLength": 1},
     "max_stake":          {"type": "number", "minimum": 0},
     "max_daily_exposure": {"type": "number", "minimum": 0},
     "max_price_age_s":    {"type": "integer", "minimum": 1},
     "kill_switch":        {"const": true},
     "rationale":          {"type": "string", "maxLength": 4000}}}');

create table kb.action_request (
    id              uuid primary key default gen_random_uuid(),
    action          text not null references kb.action_type(name),
    params          jsonb not null,
    requested_by    text not null references kb.agent(id),
    status          text not null
                    check (status in ('approved', 'rejected', 'claimed', 'executed', 'failed', 'expired')),
    decision_reason text not null,
    created_at      timestamptz not null default now(),
    decided_at      timestamptz not null default now(),
    claimed_by      text references kb.agent(id),
    claimed_at      timestamptz,
    finished_at     timestamptz
);
create index action_request_queue on kb.action_request (status, created_at);
create index action_request_agent_day on kb.action_request (requested_by, created_at);

-- GOVERNED_BY: every check that ran, and which limit row it used.
create table kb.action_check (
    request_id    uuid not null references kb.action_request(id),
    seq           integer not null,
    check_name    text not null,
    passed        boolean not null,
    detail        text,
    risk_limit_id bigint references kb.risk_limit(id),
    primary key (request_id, seq)
);
create trigger action_check_append_only before update or delete on kb.action_check
for each row execute function kb.forbid_change();

create table kb.action_event (
    id          bigint generated always as identity primary key,
    request_id  uuid not null references kb.action_request(id),
    at          timestamptz not null default now(),
    actor       text not null,
    from_status text,
    to_status   text not null,
    note        text
);
create trigger action_event_append_only before update or delete on kb.action_event
for each row execute function kb.forbid_change();

create function kb.action_request_guard() returns trigger
language plpgsql set search_path = '' as $$
begin
    if tg_op = 'DELETE' then
        raise exception 'action_request is append-only (DELETE refused)';
    end if;
    if tg_op = 'UPDATE' then
        if new.id <> old.id or new.action <> old.action or new.params <> old.params
           or new.requested_by <> old.requested_by or new.created_at <> old.created_at
           or new.decision_reason <> old.decision_reason or new.decided_at <> old.decided_at then
            raise exception 'action_request % : only status and execution fields may change', old.id;
        end if;
        if not ((old.status = 'approved' and new.status in ('claimed', 'expired'))
             or (old.status = 'claimed'  and new.status in ('executed', 'failed'))) then
            raise exception 'action_request % : % -> % is not an allowed transition',
                old.id, old.status, new.status;
        end if;
    end if;
    return new;
end $$;

create trigger action_request_guard before update or delete on kb.action_request
for each row execute function kb.action_request_guard();

create function kb.action_request_log() returns trigger
language plpgsql set search_path = '' as $$
begin
    if tg_op = 'INSERT' or new.status <> old.status then
        insert into kb.action_event (request_id, actor, from_status, to_status, note)
        values (new.id,
                coalesce(nullif(current_setting('kb.actor', true), ''), session_user),
                case when tg_op = 'UPDATE' then old.status end,
                new.status,
                nullif(current_setting('kb.event_note', true), ''));
    end if;
    return null;
end $$;

create trigger action_request_log after insert or update on kb.action_request
for each row execute function kb.action_request_log();

-- RESULTED_IN (request -> order) and TARGETS (order -> market).
create table kb.order_ticket (
    id               bigint generated always as identity primary key,
    request_id       uuid not null unique references kb.action_request(id),
    market_id        bigint not null references kb.market(id),
    side             text not null check (side in ('home', 'away', 'over', 'under')),
    line             numeric,
    limit_american   integer not null,
    stake            numeric not null check (stake > 0),
    venue            text not null,
    -- dry_run : the executor built the venue request and did not send it
    -- live    : the request was sent to the venue
    mode             text not null check (mode in ('dry_run', 'live')),
    status           text not null check (status in ('not_sent', 'submitted', 'open', 'partially_filled',
                                                     'filled', 'canceled', 'rejected')),
    venue_order_id   text,
    request_payload  jsonb not null,
    response_payload jsonb,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    check (mode = 'live' or status in ('not_sent', 'canceled'))
);

-- ---------------------------------------------------------------------------
-- Internal helpers (no EXECUTE granted to anyone).
-- ---------------------------------------------------------------------------

create function kb._caller_agent() returns kb.agent
language sql stable security definer set search_path = '' as $$
    select a.* from kb.agent a where a.db_role = session_user and a.active;
$$;

-- Current limit rows that apply to an order on this market by this agent.
create function kb._applicable_limits(p_market_id bigint, p_agent_id text)
returns setof kb.risk_limit
language sql stable security definer set search_path = '' as $$
    select l.id, l.scope, l.scope_key, l.max_stake, l.max_daily_exposure, l.max_price_age_s,
           l.kill_switch, l.set_by, l.set_at, l.reason
    from kb.risk_limit_current l
    left join kb.market m on m.id = p_market_id
    left join kb.game g on g.id = m.game_id
    where (l.scope = 'global')
       or (l.scope = 'league' and l.scope_key = g.league)
       or (l.scope = 'market' and l.scope_key = p_market_id::text)
       or (l.scope = 'agent'  and l.scope_key = p_agent_id);
$$;

-- Stake already committed today (UTC) inside one limit's scope.
create function kb._exposure_today(p_limit kb.risk_limit, p_market_id bigint, p_agent_id text)
returns numeric
language sql stable security definer set search_path = '' as $$
    select coalesce(sum((r.params ->> 'stake')::numeric), 0)
    from kb.action_request r
    join kb.market m on m.id = (r.params ->> 'market_id')::bigint
    join kb.game g on g.id = m.game_id
    where r.action = 'place_order'
      and r.status in ('approved', 'claimed', 'executed')
      and r.created_at >= (date_trunc('day', now() at time zone 'utc') at time zone 'utc')
      and case p_limit.scope
            when 'global' then true
            when 'league' then g.league = p_limit.scope_key
            when 'market' then m.id = p_market_id
            when 'agent'  then r.requested_by = p_agent_id
          end;
$$;

create function kb._latest_quote(p_market_id bigint, p_side text)
returns kb.price_snapshot
language sql stable security definer set search_path = '' as $$
    select s.* from kb.price_snapshot s
    where s.market_id = p_market_id and s.side = p_side and s.quote = 'current'
    order by s.observed_at desc, s.id desc
    limit 1;
$$;

create function kb._american_to_decimal(p integer) returns numeric
language sql immutable set search_path = '' as $$
    select case when p > 0 then 1 + p / 100.0 else 1 + 100.0 / (-p) end;
$$;

-- ---------------------------------------------------------------------------
-- propose_action: the only entry point agents have.
-- ---------------------------------------------------------------------------

create function kb.propose_action(p_action text, p_params jsonb)
returns table (request_id uuid, status text, reason text)
language plpgsql security definer set search_path = '' as $$
#variable_conflict use_column
declare
    v_agent   kb.agent;
    v_type    kb.action_type;
    v_id      uuid := gen_random_uuid();
    v_checks  jsonb := '[]'::jsonb;
    v_failed  text;
    v_market  kb.market;
    v_game    kb.game;
    v_lim     kb.risk_limit;
    v_quote   kb.price_snapshot;
    v_order   kb.order_ticket;
    v_cur     kb.risk_limit;
    v_new_id  bigint;
    v_stake   numeric;
    v_used    numeric;
    v_age_cap integer;
    v_any     boolean;
    v_status  text;
    v_key     text;
    v_field   text;
begin
    v_agent := kb._caller_agent();
    if v_agent.id is null then
        raise exception 'no active agent is registered for database role %', session_user
            using errcode = 'SP010';
    end if;
    select * into v_type from kb.action_type t where t.name = p_action;
    if not found then
        raise exception 'unknown action %', p_action using errcode = 'SP011';
    end if;
    perform set_config('kb.actor', v_agent.id, true);

    -- A small closure would be nicer; plpgsql makes do with a jsonb list.
    -- Each element: {name, passed, detail, limit_id}.

    if not (v_agent.role = any (v_type.allowed_roles)) then
        v_checks := v_checks || jsonb_build_object('name', 'role', 'passed', false,
            'detail', format('role %s may not request %s', v_agent.role, p_action));
    else
        v_checks := v_checks || jsonb_build_object('name', 'role', 'passed', true,
            'detail', v_agent.role);
    end if;

    if p_params is null or not extensions.jsonb_matches_schema(v_type.param_schema::json, p_params) then
        v_checks := v_checks || jsonb_build_object('name', 'param_schema', 'passed', false,
            'detail', 'params do not match the action''s JSON schema');
    else
        v_checks := v_checks || jsonb_build_object('name', 'param_schema', 'passed', true);
    end if;

    if not exists (select 1 from jsonb_array_elements(v_checks) c where not (c ->> 'passed')::boolean) then

        if p_action in ('propose_edge', 'place_order') then
            select * into v_market from kb.market m where m.id = (p_params ->> 'market_id')::bigint;
            if not found then
                v_checks := v_checks || jsonb_build_object('name', 'market_exists', 'passed', false,
                    'detail', format('market %s does not exist', p_params ->> 'market_id'));
            else
                select * into v_game from kb.game g where g.id = v_market.game_id;
                v_checks := v_checks || jsonb_build_object('name', 'market_open',
                    'passed', v_game.status = 'scheduled' and v_game.start_time > now(),
                    'detail', format('game %s is %s, starts %s', v_game.id, v_game.status, v_game.start_time));
                v_checks := v_checks || jsonb_build_object('name', 'side_valid',
                    'passed', case when v_market.kind = 'total' then p_params ->> 'side' in ('over', 'under')
                                   else p_params ->> 'side' in ('home', 'away') end,
                    'detail', format('%s on a %s market', p_params ->> 'side', v_market.kind));
            end if;
        end if;

        if p_action = 'place_order' and v_market.id is not null then
            -- Serialise order proposals so two agents cannot both use the last
            -- of a daily exposure budget.
            perform pg_advisory_xact_lock(hashtext('kb.place_order'));
            v_stake := (p_params ->> 'stake')::numeric;

            v_checks := v_checks || jsonb_build_object('name', 'global_limit_exists',
                'passed', exists (select 1 from kb.risk_limit_current l where l.scope = 'global'),
                'detail', 'a global risk limit must exist before any order is approved');

            v_age_cap := null;
            for v_lim in select * from kb._applicable_limits(v_market.id, v_agent.id) loop
                v_checks := v_checks || jsonb_build_object('name', 'kill_switch',
                    'passed', not v_lim.kill_switch, 'limit_id', v_lim.id,
                    'detail', format('%s:%s', v_lim.scope, v_lim.scope_key));
                if v_lim.max_stake is not null then
                    v_checks := v_checks || jsonb_build_object('name', 'max_stake',
                        'passed', v_stake <= v_lim.max_stake, 'limit_id', v_lim.id,
                        'detail', format('stake %s, max %s (%s:%s)', v_stake, v_lim.max_stake,
                                         v_lim.scope, v_lim.scope_key));
                end if;
                if v_lim.max_daily_exposure is not null then
                    v_used := kb._exposure_today(v_lim, v_market.id, v_agent.id);
                    v_checks := v_checks || jsonb_build_object('name', 'daily_exposure',
                        'passed', v_used + v_stake <= v_lim.max_daily_exposure, 'limit_id', v_lim.id,
                        'detail', format('used %s + stake %s, max %s (%s:%s)', v_used, v_stake,
                                         v_lim.max_daily_exposure, v_lim.scope, v_lim.scope_key));
                end if;
                if v_lim.max_price_age_s is not null then
                    v_age_cap := least(coalesce(v_age_cap, v_lim.max_price_age_s), v_lim.max_price_age_s);
                end if;
            end loop;
            v_age_cap := coalesce(v_age_cap, 900);

            v_quote := kb._latest_quote(v_market.id, p_params ->> 'side');
            if v_quote.id is null then
                v_checks := v_checks || jsonb_build_object('name', 'price_available', 'passed', false,
                    'detail', 'no current quote for this market and side');
            else
                v_checks := v_checks || jsonb_build_object('name', 'price_fresh',
                    'passed', now() - v_quote.observed_at <= make_interval(secs => v_age_cap),
                    'detail', format('quote %s observed %s, max age %ss', v_quote.id,
                                     v_quote.observed_at, v_age_cap));
                v_checks := v_checks || jsonb_build_object('name', 'price_within_limit',
                    'passed', v_quote.decimal_odds >= kb._american_to_decimal((p_params ->> 'limit_american')::integer),
                    'detail', format('current %s, limit %s', v_quote.american_odds, p_params ->> 'limit_american'));
                if v_market.kind <> 'moneyline' then
                    v_checks := v_checks || jsonb_build_object('name', 'line_unchanged',
                        'passed', (p_params ? 'line') and v_quote.line = (p_params ->> 'line')::numeric,
                        'detail', format('current line %s, requested %s', v_quote.line,
                                         coalesce(p_params ->> 'line', 'none')));
                end if;
            end if;
        end if;

        if p_action = 'cancel_order' then
            select o.* into v_order from kb.order_ticket o where o.id = (p_params ->> 'order_id')::bigint;
            if not found then
                v_checks := v_checks || jsonb_build_object('name', 'order_exists', 'passed', false,
                    'detail', format('order %s does not exist', p_params ->> 'order_id'));
            else
                v_checks := v_checks || jsonb_build_object('name', 'order_owner',
                    'passed', v_agent.role = 'risk_officer'
                              or exists (select 1 from kb.action_request r
                                         where r.id = v_order.request_id and r.requested_by = v_agent.id),
                    'detail', format('order %s', v_order.id));
                v_checks := v_checks || jsonb_build_object('name', 'order_cancellable',
                    'passed', v_order.status in ('not_sent', 'submitted', 'open', 'partially_filled'),
                    'detail', format('order is %s', v_order.status));
            end if;
        end if;

        if p_action = 'set_risk_limit' then
            v_key := case when p_params ->> 'scope' = 'global' then '*'
                          else p_params ->> 'scope_key' end;
            if v_key is null then
                v_checks := v_checks || jsonb_build_object('name', 'scope_key', 'passed', false,
                    'detail', 'scope_key is required unless scope is global');
            else
                select * into v_cur from kb.risk_limit_current l
                where l.scope = p_params ->> 'scope' and l.scope_key = v_key;
                v_any := false;
                foreach v_field in array array['max_stake', 'max_daily_exposure', 'max_price_age_s'] loop
                    if p_params ? v_field then
                        v_any := true;
                        v_checks := v_checks || jsonb_build_object('name', 'tightens_' || v_field,
                            'passed', (to_jsonb(v_cur) ->> v_field) is null
                                      or (p_params ->> v_field)::numeric <= (to_jsonb(v_cur) ->> v_field)::numeric,
                            'limit_id', v_cur.id,
                            'detail', format('current %s, requested %s',
                                             coalesce(to_jsonb(v_cur) ->> v_field, 'unlimited'),
                                             p_params ->> v_field));
                    end if;
                end loop;
                v_checks := v_checks || jsonb_build_object('name', 'changes_something',
                    'passed', v_any or (p_params ? 'kill_switch'),
                    'detail', 'set at least one limit or the kill switch');
            end if;
        end if;
    end if;

    select c ->> 'name' || ': ' || coalesce(c ->> 'detail', '') into v_failed
    from jsonb_array_elements(v_checks) with ordinality as x(c, n)
    where not (c ->> 'passed')::boolean
    order by n limit 1;

    if v_failed is not null then
        v_status := 'rejected';
    elsif v_type.executes then
        v_status := 'approved';
    else
        v_status := case when p_action = 'set_risk_limit' then 'executed' else 'approved' end;
    end if;

    insert into kb.action_request (id, action, params, requested_by, status, decision_reason)
    values (v_id, p_action, p_params, v_agent.id,
            case when v_status = 'executed' then 'approved' else v_status end,
            coalesce(v_failed, 'all checks passed'));

    insert into kb.action_check (request_id, seq, check_name, passed, detail, risk_limit_id)
    select v_id, n::integer, c ->> 'name', (c ->> 'passed')::boolean, c ->> 'detail',
           (c ->> 'limit_id')::bigint
    from jsonb_array_elements(v_checks) with ordinality as x(c, n);

    -- set_risk_limit takes effect inside the database, immediately.
    if v_status = 'executed' then
        insert into kb.risk_limit (scope, scope_key, max_stake, max_daily_exposure, max_price_age_s,
                                   kill_switch, set_by, reason)
        values (p_params ->> 'scope', v_key,
                coalesce((p_params ->> 'max_stake')::numeric, v_cur.max_stake),
                coalesce((p_params ->> 'max_daily_exposure')::numeric, v_cur.max_daily_exposure),
                coalesce((p_params ->> 'max_price_age_s')::integer, v_cur.max_price_age_s),
                coalesce((p_params ->> 'kill_switch')::boolean, false) or coalesce(v_cur.kill_switch, false),
                v_agent.id, coalesce(p_params ->> 'rationale', 'set_risk_limit request ' || v_id))
        returning id into v_new_id;
        perform set_config('kb.event_note', 'risk_limit ' || v_new_id, true);
        update kb.action_request r set status = 'claimed', claimed_by = v_agent.id, claimed_at = now()
        where r.id = v_id;
        update kb.action_request r set status = 'executed', finished_at = now() where r.id = v_id;
        perform set_config('kb.event_note', '', true);
    end if;

    return query select v_id, v_status, coalesce(v_failed, 'all checks passed');
end $$;

-- ---------------------------------------------------------------------------
-- Human-only: set any limit in either direction, including releasing a kill
-- switch. Grant EXECUTE only to the human role.
-- ---------------------------------------------------------------------------

create function kb.set_risk_limit_human(
    p_scope text, p_scope_key text, p_max_stake numeric, p_max_daily_exposure numeric,
    p_max_price_age_s integer, p_kill_switch boolean, p_reason text)
returns bigint
language plpgsql security definer set search_path = '' as $$
declare
    v_agent kb.agent;
    v_id    bigint;
begin
    v_agent := kb._caller_agent();
    if v_agent.id is null or v_agent.role <> 'human' then
        raise exception 'only a human may call set_risk_limit_human' using errcode = 'SP012';
    end if;
    if p_reason is null or btrim(p_reason) = '' then
        raise exception 'a reason is required' using errcode = 'SP012';
    end if;
    insert into kb.risk_limit (scope, scope_key, max_stake, max_daily_exposure, max_price_age_s,
                               kill_switch, set_by, reason)
    values (p_scope, case when p_scope = 'global' then '*' else p_scope_key end,
            p_max_stake, p_max_daily_exposure, p_max_price_age_s, coalesce(p_kill_switch, false),
            v_agent.id, p_reason)
    returning id into v_id;
    return v_id;
end $$;

-- ---------------------------------------------------------------------------
-- Stake sizing: a pure function agents may call. Fractional Kelly on the
-- current quote, capped by every max_stake that applies.
-- ---------------------------------------------------------------------------

create function kb.size_stake(p_market_id bigint, p_side text, p_model_prob numeric,
                              p_bankroll numeric, p_kelly_fraction numeric default 0.25)
returns table (stake numeric, decimal_odds numeric, edge numeric, kelly numeric, cap numeric)
language plpgsql stable security definer set search_path = '' as $$
#variable_conflict use_column
declare
    v_agent kb.agent;
    v_quote kb.price_snapshot;
    v_b     numeric;
begin
    if p_model_prob is null or p_model_prob <= 0 or p_model_prob >= 1 then
        raise exception 'model_prob must be in (0, 1)';
    end if;
    if p_bankroll is null or p_bankroll < 0 then
        raise exception 'bankroll must be >= 0';
    end if;
    if p_kelly_fraction is null or p_kelly_fraction <= 0 or p_kelly_fraction > 1 then
        raise exception 'kelly_fraction must be in (0, 1]';
    end if;
    v_agent := kb._caller_agent();
    v_quote := kb._latest_quote(p_market_id, p_side);
    if v_quote.id is null then
        raise exception 'no current quote for market % side %', p_market_id, p_side;
    end if;
    v_b    := v_quote.decimal_odds - 1;
    edge   := p_model_prob * v_quote.decimal_odds - 1;
    kelly  := greatest(edge / v_b, 0);
    select min(l.max_stake) into cap from kb._applicable_limits(p_market_id, v_agent.id) l;
    if exists (select 1 from kb._applicable_limits(p_market_id, v_agent.id) l where l.kill_switch) then
        cap := 0;
    end if;
    decimal_odds := v_quote.decimal_odds;
    stake := round(least(p_bankroll * kelly * p_kelly_fraction, coalesce(cap, 0)), 2);
    return next;
end $$;

-- ---------------------------------------------------------------------------
-- Executor-only functions.
-- ---------------------------------------------------------------------------

create function kb.claim_approved(p_limit integer default 10)
returns setof kb.action_request
language plpgsql security definer set search_path = '' as $$
declare
    v_agent kb.agent;
    r       kb.action_request;
    v_why   text;
begin
    v_agent := kb._caller_agent();
    if v_agent.id is null or v_agent.role <> 'executor' then
        raise exception 'only an executor may claim requests' using errcode = 'SP013';
    end if;
    perform set_config('kb.actor', v_agent.id, true);
    for r in
        select q.* from kb.action_request q
        join kb.action_type t on t.name = q.action and t.executes
        where q.status = 'approved'
        order by q.created_at
        limit greatest(p_limit, 0)
        for update of q skip locked
    loop
        v_why := null;
        if r.action = 'place_order' then
            -- Re-check what can change between approval and execution.
            select format('kill switch on (%s:%s)', l.scope, l.scope_key) into v_why
            from kb._applicable_limits((r.params ->> 'market_id')::bigint, r.requested_by) l
            where l.kill_switch limit 1;
            if v_why is null then
                select format('game %s is %s, starts %s', g.id, g.status, g.start_time) into v_why
                from kb.market m join kb.game g on g.id = m.game_id
                where m.id = (r.params ->> 'market_id')::bigint
                  and not (g.status = 'scheduled' and g.start_time > now());
            end if;
        end if;
        if v_why is not null then
            perform set_config('kb.event_note', v_why, true);
            update kb.action_request q set status = 'expired', finished_at = now() where q.id = r.id;
        else
            perform set_config('kb.event_note', '', true);
            update kb.action_request q set status = 'claimed', claimed_by = v_agent.id, claimed_at = now()
            where q.id = r.id returning q.* into r;
            return next r;
        end if;
    end loop;
    perform set_config('kb.event_note', '', true);
end $$;

create function kb.record_execution(
    p_request_id uuid, p_ok boolean, p_mode text, p_order_status text, p_venue text,
    p_venue_order_id text, p_request_payload jsonb, p_response_payload jsonb, p_note text)
returns bigint
language plpgsql security definer set search_path = '' as $$
declare
    v_agent kb.agent;
    r       kb.action_request;
    v_id    bigint;
    v_q     kb.price_snapshot;
begin
    v_agent := kb._caller_agent();
    if v_agent.id is null or v_agent.role <> 'executor' then
        raise exception 'only an executor may record executions' using errcode = 'SP013';
    end if;
    select * into r from kb.action_request q where q.id = p_request_id for update;
    if not found or r.status <> 'claimed' or r.claimed_by <> v_agent.id then
        raise exception 'request % is not claimed by %', p_request_id, v_agent.id using errcode = 'SP013';
    end if;
    perform set_config('kb.actor', v_agent.id, true);
    perform set_config('kb.event_note', coalesce(p_note, ''), true);

    if p_ok and r.action = 'place_order' then
        insert into kb.order_ticket (request_id, market_id, side, line, limit_american, stake, venue,
                                     mode, status, venue_order_id, request_payload, response_payload)
        values (r.id, (r.params ->> 'market_id')::bigint, r.params ->> 'side',
                (r.params ->> 'line')::numeric, (r.params ->> 'limit_american')::integer,
                (r.params ->> 'stake')::numeric, p_venue, p_mode, p_order_status, p_venue_order_id,
                coalesce(p_request_payload, '{}'::jsonb), p_response_payload)
        returning id into v_id;
    elsif p_ok and r.action = 'cancel_order' then
        update kb.order_ticket o
        set status = 'canceled', updated_at = now(),
            response_payload = coalesce(p_response_payload, o.response_payload)
        where o.id = (r.params ->> 'order_id')::bigint
        returning o.id into v_id;
    end if;

    update kb.action_request q
    set status = case when p_ok then 'executed' else 'failed' end, finished_at = now()
    where q.id = r.id;
    perform set_config('kb.event_note', '', true);
    return v_id;
end $$;
