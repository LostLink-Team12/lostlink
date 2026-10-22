package pgxtls

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
)

// Exercise the actual KEDA driver, not a Python/libpq substitute. SQL/authentication
// and AWS routing remain cloud acceptance work; these tests isolate TLS negotiation.
func TestPostgresScalerTrust(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer server.Close()
	root := filepath.Join(t.TempDir(), "rds-test-ca.pem")
	if err := os.WriteFile(root, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	ca := &x509.Certificate{
		SerialNumber: big.NewInt(2), Subject: pkix.Name{CommonName: "untrusted-test-CA"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign,
	}
	der, err := x509.CreateCertificate(rand.Reader, ca, ca, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	wrong := filepath.Join(t.TempDir(), "wrong-ca.pem")
	if err := os.WriteFile(wrong, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0600); err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct{ name, ca, host, failure string }{
		{"trusted-host", root, "example.com", ""},
		{"wrong-CA", wrong, "example.com", "ca"},
		{"wrong-host", root, "wrong.invalid", "host"},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Setenv("PGSSLROOTCERT", test.ca)
			t.Setenv("PGSSLCERT", "")
			t.Setenv("PGSSLKEY", "")
			// This is the keyword DSN form constructed by KEDA's PostgreSQL scaler.
			config, err := pgconn.ParseConfig("host=" + test.host + " port=5432 user=scaler dbname=lostlink sslmode=verify-full")
			if err != nil {
				t.Fatal(err)
			}
			if config.TLSConfig == nil || config.TLSConfig.InsecureSkipVerify || config.TLSConfig.RootCAs == nil {
				t.Fatal("pgx must verify the server using the CA from PGSSLROOTCERT")
			}
			for _, fallback := range config.Fallbacks {
				if fallback.TLSConfig == nil {
					t.Fatal("verify-full must not allow a plaintext fallback")
				}
			}
			conn, err := tls.DialWithDialer(&net.Dialer{Timeout: 3 * time.Second}, "tcp", server.Listener.Addr().String(), config.TLSConfig)
			if conn != nil {
				conn.Close()
			}
			if test.failure == "" {
				if err != nil {
					t.Fatal(err)
				}
				return
			}
			var caError x509.UnknownAuthorityError
			var hostError x509.HostnameError
			if test.failure == "ca" && !errors.As(err, &caError) {
				t.Fatalf("expected untrusted-CA rejection, got %v", err)
			}
			if test.failure == "host" && !errors.As(err, &hostError) {
				t.Fatalf("expected hostname rejection, got %v", err)
			}
		})
	}
	t.Run("missing-CA-fails-closed", func(t *testing.T) {
		t.Setenv("PGSSLROOTCERT", filepath.Join(t.TempDir(), "missing.pem"))
		if _, err := pgconn.ParseConfig("host=example.com user=scaler dbname=lostlink sslmode=verify-full"); err == nil {
			t.Fatal("an unavailable configured CA must prevent connection creation")
		}
	})
}
