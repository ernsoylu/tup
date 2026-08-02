package core

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/spf13/viper"
)

type Config struct {
	TelegramAPIID   int    `mapstructure:"TELEGRAM_API_ID"`
	TelegramAPIHash string `mapstructure:"TELEGRAM_API_HASH"`
}

var AppConfig Config

func InitConfig() error {
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}

	viper.SetConfigFile(filepath.Join(home, ".tup", ".env"))
	viper.SetConfigType("env")

	// Default to environment variables if present
	viper.AutomaticEnv()

	// It's okay if .env doesn't exist yet (e.g. first run); the user
	// will be prompted to configure later.
	_ = viper.ReadInConfig()

	if err := viper.Unmarshal(&AppConfig); err != nil {
		return fmt.Errorf("failed to unmarshal config: %w", err)
	}

	return nil
}
