-- ============================================
-- ParkingHunter MVP — Full Supabase Schema
-- ============================================
-- RESET + CREATE: drops everything and rebuilds from scratch.
-- Safe to run multiple times.
-- ============================================

-- ============================================
-- STEP 1: DROP EVERYTHING
-- ============================================
DROP TRIGGER IF EXISTS trigger_spots_set_geom ON spots;
DROP TRIGGER IF EXISTS trigger_seeker_sessions_set_geom ON seeker_sessions;
DROP TRIGGER IF EXISTS trigger_garages_set_geom ON garages;

DROP FUNCTION IF EXISTS spots_set_geom();
DROP FUNCTION IF EXISTS seeker_sessions_set_geom();
DROP FUNCTION IF EXISTS garages_set_geom();
DROP FUNCTION IF EXISTS increment_hunter_points(BIGINT);
DROP FUNCTION IF EXISTS find_nearby_seekers(DOUBLE PRECISION, DOUBLE PRECISION, INTEGER);
DROP FUNCTION IF EXISTS find_nearest_cheap_garage(DOUBLE PRECISION, DOUBLE PRECISION, INTEGER);
DROP FUNCTION IF EXISTS cleanup_expired_spots();
DROP FUNCTION IF EXISTS cleanup_expired_sessions();

DROP TABLE IF EXISTS spots CASCADE;
DROP TABLE IF EXISTS seeker_sessions CASCADE;
DROP TABLE IF EXISTS garages CASCADE;
DROP TABLE IF EXISTS users CASCADE;

-- ============================================
-- STEP 2: CREATE FRESH
-- ============================================

-- Enable PostGIS for geo queries
CREATE EXTENSION IF NOT EXISTS postgis;

-- ============================================
-- USERS TABLE
-- ============================================
CREATE TABLE users (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    telegram_username TEXT,
    telegram_first_name TEXT,
    role TEXT DEFAULT 'none', -- 'hunter', 'seeker', 'none'
    hunter_points INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_users_telegram_id ON users(telegram_id);

-- ============================================
-- SPOTS TABLE
-- ============================================
CREATE TABLE spots (
    id BIGSERIAL PRIMARY KEY,
    hunter_telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
    photo_url TEXT,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    geom GEOMETRY(Point, 4326), -- PostGIS point for geo queries
    status TEXT DEFAULT 'active', -- 'active', 'taken', 'expired'
    reserved_by BIGINT, -- seeker telegram_id who tapped Navigate
    notified_seekers BIGINT[] DEFAULT '{}', -- track who was already notified
    current_notify_index INTEGER DEFAULT 0, -- which seeker in the queue
    last_notified_at TIMESTAMPTZ, -- for staggered notifications (60s between seekers)
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '10 minutes')
);

CREATE INDEX idx_spots_status ON spots(status);
CREATE INDEX idx_spots_geom ON spots USING GIST(geom);
CREATE INDEX idx_spots_expires_at ON spots(expires_at);

-- Auto-generate geom from lat/lng on insert
CREATE OR REPLACE FUNCTION spots_set_geom()
RETURNS TRIGGER AS $$
BEGIN
    NEW.geom := ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_spots_set_geom
    BEFORE INSERT OR UPDATE ON spots
    FOR EACH ROW
    EXECUTE FUNCTION spots_set_geom();

-- ============================================
-- SEEKER SESSIONS TABLE
-- ============================================
CREATE TABLE seeker_sessions (
    id BIGSERIAL PRIMARY KEY,
    seeker_telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    geom GEOMETRY(Point, 4326),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '30 minutes')
);

CREATE INDEX idx_seeker_sessions_active ON seeker_sessions(is_active) WHERE is_active = TRUE;
CREATE INDEX idx_seeker_sessions_geom ON seeker_sessions USING GIST(geom);
CREATE INDEX idx_seeker_sessions_expires ON seeker_sessions(expires_at);

-- Auto-generate geom from lat/lng on insert/update
CREATE OR REPLACE FUNCTION seeker_sessions_set_geom()
RETURNS TRIGGER AS $$
BEGIN
    NEW.geom := ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_seeker_sessions_set_geom
    BEFORE INSERT OR UPDATE ON seeker_sessions
    FOR EACH ROW
    EXECUTE FUNCTION seeker_sessions_set_geom();

-- ============================================
-- GARAGES TABLE (static data)
-- ============================================
CREATE TABLE garages (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    geom GEOMETRY(Point, 4326),
    price_per_hour NUMERIC(6,2) NOT NULL, -- in ILS
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_garages_geom ON garages USING GIST(geom);

-- Auto-generate geom
CREATE OR REPLACE FUNCTION garages_set_geom()
RETURNS TRIGGER AS $$
BEGIN
    NEW.geom := ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_garages_set_geom
    BEFORE INSERT OR UPDATE ON garages
    FOR EACH ROW
    EXECUTE FUNCTION garages_set_geom();

-- ============================================
-- RPC FUNCTIONS
-- ============================================

-- Atomic hunter points increment (no race condition)
CREATE OR REPLACE FUNCTION increment_hunter_points(p_telegram_id BIGINT)
RETURNS INTEGER AS $$
DECLARE
    new_points INTEGER;
BEGIN
    UPDATE users
    SET hunter_points = hunter_points + 1,
        updated_at = NOW()
    WHERE telegram_id = p_telegram_id
    RETURNING hunter_points INTO new_points;

    RETURN COALESCE(new_points, 0);
END;
$$ LANGUAGE plpgsql;

-- Find closest active seekers within radius (in meters) of a spot
CREATE OR REPLACE FUNCTION find_nearby_seekers(
    spot_lat DOUBLE PRECISION,
    spot_lng DOUBLE PRECISION,
    radius_meters INTEGER DEFAULT 500
)
RETURNS TABLE (
    session_id BIGINT,
    seeker_telegram_id BIGINT,
    distance_meters DOUBLE PRECISION
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        s.id AS session_id,
        s.seeker_telegram_id,
        ST_Distance(
            s.geom::geography,
            ST_SetSRID(ST_MakePoint(spot_lng, spot_lat), 4326)::geography
        ) AS distance_meters
    FROM seeker_sessions s
    WHERE s.is_active = TRUE
      AND s.expires_at > NOW()
      AND ST_DWithin(
            s.geom::geography,
            ST_SetSRID(ST_MakePoint(spot_lng, spot_lat), 4326)::geography,
            radius_meters
          )
    ORDER BY distance_meters ASC;
END;
$$ LANGUAGE plpgsql;

-- Find cheapest nearby garage
CREATE OR REPLACE FUNCTION find_nearest_cheap_garage(
    seeker_lat DOUBLE PRECISION,
    seeker_lng DOUBLE PRECISION,
    limit_count INTEGER DEFAULT 1
)
RETURNS TABLE (
    garage_id BIGINT,
    name TEXT,
    address TEXT,
    price_per_hour NUMERIC,
    distance_meters DOUBLE PRECISION
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        g.id AS garage_id,
        g.name,
        g.address,
        g.price_per_hour,
        ST_Distance(
            g.geom::geography,
            ST_SetSRID(ST_MakePoint(seeker_lng, seeker_lat), 4326)::geography
        ) AS distance_meters
    FROM garages g
    WHERE g.is_active = TRUE
    ORDER BY distance_meters ASC, g.price_per_hour ASC
    LIMIT limit_count;
END;
$$ LANGUAGE plpgsql;

-- Cleanup: expire old spots
CREATE OR REPLACE FUNCTION cleanup_expired_spots()
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    WITH deleted AS (
        DELETE FROM spots
        WHERE expires_at < NOW()
          AND status != 'taken'
        RETURNING id
    )
    SELECT COUNT(*) INTO deleted_count FROM deleted;

    DELETE FROM spots
    WHERE expires_at < NOW();

    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- Cleanup: expire old seeker sessions
CREATE OR REPLACE FUNCTION cleanup_expired_sessions()
RETURNS INTEGER AS $$
DECLARE
    closed_count INTEGER;
BEGIN
    WITH updated AS (
        UPDATE seeker_sessions
        SET is_active = FALSE
        WHERE expires_at < NOW()
          AND is_active = TRUE
        RETURNING id
    )
    SELECT COUNT(*) INTO closed_count FROM updated;

    RETURN closed_count;
END;
$$ LANGUAGE plpgsql;

-- ============================================
-- SAMPLE GARAGE DATA (Tel Aviv)
-- ==============