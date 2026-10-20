package workbuddyhost

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"testing"
)

func TestDesktopStatusRejectsForeignAndAmbiguousEndpoints(t *testing.T) {
	for _, test := range []struct {
		body string
		ok   bool
	}{
		{`[{"type":"page","title":"WorkBuddy"}]`, true},
		{`[{"type":"page","title":"Other"}]`, false},
		{`[{"type":"page","title":"WorkBuddy"},{"type":"page","title":"WorkBuddy"}]`, false},
		{`not json`, false},
	} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte(test.body)) }))
		address, _ := url.Parse(server.URL)
		port, _ := strconv.Atoi(address.Port())
		ready, err := desktopStatus(context.Background(), port)
		if ready != test.ok || (err == nil) != test.ok {
			t.Fatalf("body=%s ready=%v error=%v", test.body, ready, err)
		}
		if test.ok {
			if err := PrepareDesktop(context.Background(), port, false); err != nil {
				t.Fatal(err)
			}
		}
		server.Close()
	}
}
