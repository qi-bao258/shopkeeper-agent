# 前端应用镜像
#
# 两阶段：Node 里构建静态产物 → Nginx 托管并反代 /api 到后端容器。
#
# 为什么不用 vite dev server：
#   dev server 只适合开发调试，生产/长期运行场景用静态构建 + Nginx 更稳，
#   且不依赖 node_modules 的 pnpm 链接（容器内重新安装，与宿主机无关）。

FROM node:22-alpine AS builder

WORKDIR /app

# 只拷贝依赖清单，利用层缓存
COPY package.json pnpm-lock.yaml ./

# 用 corepack 激活声明版本的 pnpm（package.json 里 packageManager 指定）
RUN corepack enable && corepack prepare pnpm@10.33.0 --activate || true
RUN pnpm install --frozen-lockfile --prod=false || npm install

# 拷贝源码并构建
COPY . .
RUN pnpm build || npm run build


FROM nginx:alpine AS runtime

# 自定义 Nginx 配置：SPA fallback + /api 反代
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
