package coreapi

import (
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"testing/iotest"

	"lazymind/agentconnector/internal/credentials"
)

type requestErrorTransport func(*http.Request) (*http.Response, error)

func (f requestErrorTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestDoJSONDistinguishesLocalRejectionFromUnconfirmedResponse(t *testing.T) {
	for _, scenario := range []string{"encoding", "credentials", "transport", "redirect", "response-read", "response-decode"} {
		t.Run(scenario, func(t *testing.T) {
			store, err := credentials.NewStore(t.TempDir(), "http://core.invalid")
			if err != nil {
				t.Fatal(err)
			}
			if err := store.Save(credentials.Credentials{ServerURL: "http://core.invalid", AccessToken: "test-access", RefreshToken: "test-refresh"}); err != nil {
				t.Fatal(err)
			}
			client, err := New(store)
			if err != nil {
				t.Fatal(err)
			}
			calls := 0
			client.http.Transport.(*authenticatedTransport).base = requestErrorTransport(func(r *http.Request) (*http.Response, error) {
				calls++
				if scenario == "transport" {
					return nil, io.ErrUnexpectedEOF
				}
				if scenario == "redirect" {
					return &http.Response{StatusCode: http.StatusTemporaryRedirect,
						Header: http.Header{"Location": []string{"http://other.invalid/mutation"}},
						Body:   io.NopCloser(strings.NewReader(""))}, nil
				}
				var body io.Reader = strings.NewReader(`{"count":"invalid"}`)
				if scenario == "response-read" {
					body = iotest.ErrReader(io.ErrUnexpectedEOF)
				}
				return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: io.NopCloser(body)}, nil
			})
			var input any = map[string]any{"value": "valid"}
			if scenario == "encoding" {
				input = make(chan int)
			}
			if scenario == "credentials" {
				if err := os.WriteFile(filepath.Join(store.Directory(), "credentials.json"), []byte("invalid-json"), 0600); err != nil {
					t.Fatal(err)
				}
			}
			var output struct {
				Count int `json:"count"`
			}
			err = client.DoJSON(t.Context(), http.MethodPost, "/mutation", input, &output)
			if err == nil {
				t.Fatal("expected request failure")
			}
			var notSent *RequestNotSentError
			local := scenario == "encoding" || scenario == "credentials"
			if errors.As(err, &notSent) != local {
				t.Fatalf("wrong delivery classification: %T %v", err, err)
			}
			wantCalls := 1
			if local {
				wantCalls = 0
			}
			if calls != wantCalls {
				t.Fatalf("transport calls=%d, want=%d", calls, wantCalls)
			}
			if (scenario == "transport" || scenario == "response-read") && !errors.Is(err, io.ErrUnexpectedEOF) {
				t.Fatalf("lost original transport failure: %v", err)
			}
		})
	}
}
