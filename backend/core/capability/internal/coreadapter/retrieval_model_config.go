package coreadapter

import (
	"context"
	"strings"

	"gorm.io/gorm"

	"lazymind/core/capability"
	"lazymind/core/modelconfig"
)

var retrievalModelRoles = [...]string{"embed_main", "reranker"}

type RetrievalModelConfigLoader interface {
	LoadRetrievalModelConfig(context.Context, string) (map[string]any, error)
}

type DBBackedRetrievalModelConfigLoader struct{ db *gorm.DB }

func NewDBBackedRetrievalModelConfigLoader(db *gorm.DB) (*DBBackedRetrievalModelConfigLoader, error) {
	if db == nil {
		return nil, capability.NewError(capability.Internal, "knowledge.search.models.new", "gorm db is required", false, nil)
	}
	return &DBBackedRetrievalModelConfigLoader{db: db}, nil
}

func (l *DBBackedRetrievalModelConfigLoader) LoadRetrievalModelConfig(ctx context.Context, userID string) (map[string]any, error) {
	config, err := modelconfig.LoadOwnedToolModels(ctx, l.db, strings.TrimSpace(userID), retrievalModelRoles[:])
	if err != nil {
		return nil, capability.NewError(capability.Unavailable, "knowledge.search.models", "load retrieval model configuration failed", true, err)
	}
	filtered := filterRetrievalModelConfig(config)
	if filtered["embed_main"] == nil {
		return nil, capability.NewError(capability.Unavailable, "knowledge.search.models", "semantic retrieval requires an embedding model; configure it or use mode=keyword", false, nil)
	}
	return filtered, nil
}

func filterRetrievalModelConfig(config map[string]any) map[string]any {
	filtered := make(map[string]any, len(retrievalModelRoles))
	for _, role := range retrievalModelRoles {
		if roleConfig, ok := config[role]; ok {
			filtered[role] = roleConfig
		}
	}
	return filtered
}
