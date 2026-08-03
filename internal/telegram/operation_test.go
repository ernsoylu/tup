package telegram

import (
	"testing"
	"time"
)

func TestOperationEncodeDecode(t *testing.T) {
	op := &Operation{
		Version:   1,
		PrevHash:  "parent_hash_123",
		Op:        OpCP,
		Path:      "/docs/report.pdf",
		Size:      2048,
		Sha256:    "dummy_sha256_hash",
		Timestamp: time.Now().Unix(),
	}

	payload, err := op.Encode()
	if err != nil {
		t.Fatalf("Failed to encode operation: %v", err)
	}

	if op.Hash == "" {
		t.Errorf("Expected op.Hash to be calculated, got empty string")
	}

	decoded, err := DecodeOperation(payload)
	if err != nil {
		t.Fatalf("Failed to decode operation: %v", err)
	}

	if decoded.Op != OpCP {
		t.Errorf("Expected Op %s, got %s", OpCP, decoded.Op)
	}

	if decoded.Path != "/docs/report.pdf" {
		t.Errorf("Expected Path /docs/report.pdf, got %s", decoded.Path)
	}

	if decoded.Hash != op.Hash {
		t.Errorf("Expected Hash %s, got %s", op.Hash, decoded.Hash)
	}
}
