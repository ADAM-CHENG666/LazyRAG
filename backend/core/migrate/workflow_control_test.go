package migrate

import (
	"database/sql"
	"os"
	"path/filepath"
	"testing"
)

func TestWorkflowControlMigrationPreservesNativeRuns(t *testing.T) {
	for _, driver := range []string{"sqlite", "postgres"} {
		t.Run(driver, func(t *testing.T) {
			var db *sql.DB
			if driver == "sqlite" {
				db = openRawSQLite(t, filepath.Join(t.TempDir(), "control.db"))
			} else {
				dsn := os.Getenv(migrationPostgresDSNEnv)
				if dsn == "" {
					t.Skip("PostgreSQL integration DSN required")
				}
				db = createTemporaryPostgresDatabase(t, dsn, "workflow_control")
			}
			catalog, err := (&Runner{dir: "../migrations"}).loadCatalog()
			if err != nil {
				t.Fatal(err)
			}
			for _, mode := range catalog.Modes[:len(catalog.Modes)-1] {
				execMigrationFileForDriver(t, db, mode.Aggregate.UpPath, driver)
			}
			var additions []migrationFile
			for _, migration := range catalog.Modes[len(catalog.Modes)-1].Dev {
				if migration.FileVersion == 20260908093904 || migration.FileVersion == 20260909053754 || migration.FileVersion == 20260920021806 {
					additions = append(additions, migration)
				} else {
					execMigrationFileForDriver(t, db, migration.UpPath, driver)
				}
			}
			if len(additions) != 3 {
				t.Fatal("missing external workflow migrations")
			}
			if _, err := db.Exec(`INSERT INTO plugin_sessions(id,conversation_id,plugin_id,status,created_at,updated_at) VALUES ('native','conv','writer','active',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)`); err != nil {
				t.Fatal(err)
			}
			for round := 0; round < 2; round++ {
				for _, migration := range additions {
					execMigrationFileForDriver(t, db, migration.UpPath, driver)
				}
				var protocol, binding, status string
				if err := db.QueryRow(`SELECT control_protocol,control_binding_json,status FROM plugin_sessions WHERE id='native'`).Scan(&protocol, &binding, &status); err != nil {
					t.Fatal(err)
				}
				if protocol != "" || binding != "{}" || status != "active" {
					t.Fatalf("native run changed: %q %q %q", protocol, binding, status)
				}
				for i := len(additions) - 1; i >= 0; i-- {
					execMigrationFileForDriver(t, db, additions[i].DownPath, driver)
				}
				if err := db.QueryRow(`SELECT status FROM plugin_sessions WHERE id='native'`).Scan(&status); err != nil || status != "active" {
					t.Fatalf("rollback lost native run: %s %v", status, err)
				}
			}
		})
	}
}

func TestExternalPreferencesSQLiteAggregateMatchesDev(t *testing.T) {
	catalog, err := (&Runner{dir: "../migrations"}).loadCatalog()
	if err != nil {
		t.Fatal(err)
	}
	release := openRawSQLite(t, filepath.Join(t.TempDir(), "release.db"))
	dev := openRawSQLite(t, filepath.Join(t.TempDir(), "dev.db"))
	for i, mode := range catalog.Modes {
		execMigrationFileForDriver(t, release, mode.Aggregate.UpPath, "sqlite")
		if i == len(catalog.Modes)-1 {
			for _, migration := range mode.Dev {
				execMigrationFileForDriver(t, dev, migration.UpPath, "sqlite")
			}
		} else {
			execMigrationFileForDriver(t, dev, mode.Aggregate.UpPath, "sqlite")
		}
	}
	var releaseSQL, devSQL string
	query := `SELECT sql FROM sqlite_master WHERE type='table' AND name='external_workflow_approval_preferences'`
	if err := release.QueryRow(query).Scan(&releaseSQL); err != nil {
		t.Fatal(err)
	}
	if err := dev.QueryRow(query).Scan(&devSQL); err != nil {
		t.Fatal(err)
	}
	if releaseSQL != devSQL {
		t.Fatalf("external preference schemas differ: %s / %s", releaseSQL, devSQL)
	}
}
