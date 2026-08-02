package cli

import (
	"encoding/json"
	"fmt"
	"net/http"
	"runtime"

	"github.com/minio/selfupdate"
	"github.com/pterm/pterm"
	"github.com/spf13/cobra"
)

var updateCmd = &cobra.Command{
	Use:   "update",
	Short: "Update tup to the latest version",
	Run: func(cmd *cobra.Command, args []string) {
		pterm.Info.Println("Checking for latest release...")

		// Fetch latest release from GitHub API
		resp, err := http.Get("https://api.github.com/repos/ernsoylu/tup/releases/latest")
		if err != nil {
			pterm.Error.Println("Failed to check for updates:", err)
			return
		}
		defer func() { _ = resp.Body.Close() }()

		var release struct {
			TagName string `json:"tag_name"`
			Assets  []struct {
				Name               string `json:"name"`
				BrowserDownloadURL string `json:"browser_download_url"`
			} `json:"assets"`
		}

		if err := json.NewDecoder(resp.Body).Decode(&release); err != nil {
			pterm.Error.Println("Failed to parse GitHub response:", err)
			return
		}

		// Determine target binary name based on OS/Arch
		osName := runtime.GOOS
		archName := runtime.GOARCH
		targetBinary := fmt.Sprintf("tup_%s_%s", osName, archName)
		if osName == "windows" {
			targetBinary += ".exe"
		}

		var downloadURL string
		for _, asset := range release.Assets {
			if asset.Name == targetBinary {
				downloadURL = asset.BrowserDownloadURL
				break
			}
		}

		if downloadURL == "" {
			pterm.Error.Printf("No binary found for %s/%s in release %s\n", osName, archName, release.TagName)
			return
		}

		pterm.Info.Printf("Downloading %s...\n", release.TagName)

		spinner, _ := pterm.DefaultSpinner.Start("Applying update...")
		defer func() { _ = spinner.Stop() }()

		binResp, err := http.Get(downloadURL)
		if err != nil {
			spinner.Fail("Download failed: ", err)
			return
		}
		defer func() { _ = binResp.Body.Close() }()

		err = selfupdate.Apply(binResp.Body, selfupdate.Options{})
		if err != nil {
			spinner.Fail("Failed to apply update: ", err)
			return
		}

		spinner.Success(fmt.Sprintf("Successfully updated to %s", release.TagName))
	},
}

func init() {
	RootCmd.AddCommand(updateCmd)
}
