package codexruntime

import "os"

func ownedByCurrentUser(info os.FileInfo) bool { return true }
