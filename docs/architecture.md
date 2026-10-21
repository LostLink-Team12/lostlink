# Architecture overview

Source: Team 12 project proposal (4 Oct 2026).

2026-10-20 后端实施补充：身份、租户、事件与服务间契约以[后端接入指南](team-alignment.md)和[API 契约](api/README.md)为准。`item.registered` 在照片上传确认后发布；失物报告由 matching 服务拥有；跨场馆搜索使用公开读模型。

UC3 落地修订：Embedding 计算能力与 Matching 同属 `matching` schema、不同进程，不跨 schema 写向量。SQS 消费先持久化任务再确认；任务进程完成 CLIP、向量/outbox 原子写入。Item 接受结果后才发布 available 目录；向量完成及 available 快照两种触发均会重新匹配，不能仅凭 item.embedded 公开物品。API 展示与通知发送前再核验 Item。部署伸缩约束见 [Matching README](../services/matching-service/README.md)，下方原始流程保留为 proposal 概览。

## Platform roles

| Role | Who | How they interact |
| --- | --- | --- |
| Producers | Venues (malls, MRT stations, campuses, event venues) | Venue Portal, bulk-import API |
| Consumers | Owners | Owner Web App: report, search by text or photo, claim |
| Partners | Couriers, insurers (simulated) | Partner API (OAuth2 client credentials), webhooks on domain events |
| Seed 1 | Found-item matching | UC1-UC5, UC7 |
| Seed 2 | Return delivery | UC6, built only on platform APIs and events |

## Bounded contexts and services

Venue and Tenant · Item Catalogue · Media and Embedding · Matching · Claim and Verification · Notification · Return Delivery · Analytics.
See `services/README.md` for owners.

## Request and event flow (Seed 1)

1. 场馆员工登记物品：item-service 保存元数据并返回预签名上传 URL；客户端上传后确认，服务校验 S3 对象，再通过事务发件箱发布 `item.registered`。
2. embedding-worker consumes the SQS job, computes CLIP image and text embeddings, stores them in pgvector, publishes `item.embedded`.
3. Owner searches: matching-service embeds the query and runs a top-k vector search filtered by category, date and venue.
4. Open lost reports are re-matched on `item.embedded`; a hit publishes `match.found`, and notification-service emails the owner.
5. Owner claims: claim-service asks verification questions on hidden details; venue approves; a QR handover code is issued.

## Deployment

All services run as stateless containers on Amazon EKS across 2 availability zones, behind API Gateway, WAF and Cognito.
State lives in managed services: RDS PostgreSQL + pgvector, S3, SQS, SNS, ElastiCache, SES.
