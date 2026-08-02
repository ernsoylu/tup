package main

import (
	"log"
	"os"

	"github.com/ernsoylu/tup/internal/cli"
	"github.com/ernsoylu/tup/internal/core"
)

func main() {
	if err := core.InitConfig(); err != nil {
		log.Fatalf("Failed to load config: %v", err)
	}
	if err := core.InitDB(); err != nil {
		log.Fatalf("Failed to init database: %v", err)
	}

	if err := cli.RootCmd.Execute(); err != nil {
		os.Exit(1)
	}
}
