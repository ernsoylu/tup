package cli

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"io"
	"testing"
)

func TestExtractBinaryTarGz(t *testing.T) {
	var raw bytes.Buffer
	gz := gzip.NewWriter(&raw)
	tw := tar.NewWriter(gz)
	payload := []byte("fake-tup-binary")
	hdr := &tar.Header{Name: "tup", Mode: 0755, Size: int64(len(payload))}
	if err := tw.WriteHeader(hdr); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write(payload); err != nil {
		t.Fatal(err)
	}
	_ = tw.Close()
	_ = gz.Close()

	r, err := extractBinary(bytes.NewReader(raw.Bytes()), "tup_darwin_amd64.tar.gz")
	if err != nil {
		t.Fatal(err)
	}
	got, err := io.ReadAll(r)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatalf("got %q, want %q", got, payload)
	}
}

func TestExtractBinaryZip(t *testing.T) {
	var raw bytes.Buffer
	zw := zip.NewWriter(&raw)
	payload := []byte("fake-tup-exe")
	w, err := zw.Create("tup.exe")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := w.Write(payload); err != nil {
		t.Fatal(err)
	}
	_ = zw.Close()

	r, err := extractBinary(bytes.NewReader(raw.Bytes()), "tup_windows_amd64.zip")
	if err != nil {
		t.Fatal(err)
	}
	got, err := io.ReadAll(r)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatalf("got %q, want %q", got, payload)
	}
}

func TestExtractBinaryBare(t *testing.T) {
	payload := []byte("raw-binary")
	r, err := extractBinary(bytes.NewReader(payload), "tup_linux_amd64")
	if err != nil {
		t.Fatal(err)
	}
	got, err := io.ReadAll(r)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatalf("got %q, want %q", got, payload)
	}
}
