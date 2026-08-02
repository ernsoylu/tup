package cli

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"runtime"
	"strings"

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

		// GoReleaser publishes archives: tup_<os>_<arch>.tar.gz (or .zip on Windows)
		osName := runtime.GOOS
		archName := runtime.GOARCH
		prefix := fmt.Sprintf("tup_%s_%s", osName, archName)

		var downloadURL, assetName string
		for _, asset := range release.Assets {
			name := asset.Name
			if name == prefix ||
				name == prefix+".tar.gz" ||
				name == prefix+".zip" ||
				(osName == "windows" && name == prefix+".exe") {
				downloadURL = asset.BrowserDownloadURL
				assetName = name
				break
			}
		}

		if downloadURL == "" {
			pterm.Error.Printf("No binary found for %s/%s in release %s\n", osName, archName, release.TagName)
			return
		}

		pterm.Info.Printf("Downloading %s (%s)...\n", release.TagName, assetName)

		spinner, _ := pterm.DefaultSpinner.Start("Applying update...")
		defer func() { _ = spinner.Stop() }()

		binResp, err := http.Get(downloadURL)
		if err != nil {
			spinner.Fail("Download failed: ", err)
			return
		}
		defer func() { _ = binResp.Body.Close() }()

		body, err := io.ReadAll(binResp.Body)
		if err != nil {
			spinner.Fail("Failed to read download: ", err)
			return
		}

		binReader, err := extractBinary(bytes.NewReader(body), assetName)
		if err != nil {
			spinner.Fail("Failed to extract binary: ", err)
			return
		}

		err = selfupdate.Apply(binReader, selfupdate.Options{})
		if err != nil {
			spinner.Fail("Failed to apply update: ", err)
			return
		}

		spinner.Success(fmt.Sprintf("Successfully updated to %s", release.TagName))
	},
}

// extractBinary returns a reader for the tup executable from a raw binary or archive.
func extractBinary(r io.Reader, assetName string) (io.Reader, error) {
	switch {
	case strings.HasSuffix(assetName, ".tar.gz"):
		return extractFromTarGz(r)
	case strings.HasSuffix(assetName, ".zip"):
		return extractFromZip(r)
	default:
		// Bare binary asset
		return r, nil
	}
}

func extractFromTarGz(r io.Reader) (io.Reader, error) {
	gz, err := gzip.NewReader(r)
	if err != nil {
		return nil, err
	}
	defer func() { _ = gz.Close() }()

	tr := tar.NewReader(gz)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
		name := hdr.Name
		if name == "tup" || name == "tup.exe" || strings.HasSuffix(name, "/tup") || strings.HasSuffix(name, "/tup.exe") {
			var buf bytes.Buffer
			if _, err := io.Copy(&buf, tr); err != nil {
				return nil, err
			}
			return &buf, nil
		}
	}
	return nil, fmt.Errorf("archive does not contain tup binary")
}

func extractFromZip(r io.Reader) (io.Reader, error) {
	data, err := io.ReadAll(r)
	if err != nil {
		return nil, err
	}
	zr, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return nil, err
	}
	for _, f := range zr.File {
		name := f.Name
		if name == "tup" || name == "tup.exe" || strings.HasSuffix(name, "/tup") || strings.HasSuffix(name, "/tup.exe") {
			rc, err := f.Open()
			if err != nil {
				return nil, err
			}
			var buf bytes.Buffer
			_, copyErr := io.Copy(&buf, rc)
			_ = rc.Close()
			if copyErr != nil {
				return nil, copyErr
			}
			return &buf, nil
		}
	}
	return nil, fmt.Errorf("archive does not contain tup binary")
}

func init() {
	RootCmd.AddCommand(updateCmd)
}
