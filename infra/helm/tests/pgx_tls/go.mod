module lostlink.local/keda-tls-check

go 1.26.0

// Same PostgreSQL driver version as KEDA 2.17.2; this is a test-only module.
require github.com/jackc/pgx/v5 v5.7.4

require (
	github.com/jackc/pgpassfile v1.0.0 // indirect
	github.com/jackc/pgservicefile v0.0.0-20240606120523-5a60cdf6a761 // indirect
	golang.org/x/crypto v0.58.0 // indirect
	golang.org/x/text v0.43.0 // indirect
)
