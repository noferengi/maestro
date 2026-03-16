# Kanban Board UI Wireframes

## Overview
This document contains wireframe specifications for the Kanban board interface in TheMaestro project.

## 1. Layout Structure

### 1.1 Overall Layout
```
┌─────────────────────────────────────────────────────────────────┐
│  SIDEBAR (240px)                    MAIN CONTENT (flex)          │
│  ┌─────────────────────────────┐  ┌──────────────────────────┐ │
│  │ Projects                    │  │ Board Header             │ │
│  │ - TheMaestro (active)       │  │ Title: TheMaestro        │ │
│  │ - ProjectAlpha              │  │ Project Selector         │ │
│  │ - ProjectBeta               │  │ Refresh Button           │ │
│  │ + New Project               │  └──────────────────────────┘ │
│  │                            │                                │
│  │ Global Config              │  ┌──────────────────────────┐ │
│  │ - LLM Endpoints            │  │ KANBAN BOARD             │ │
│  │ - Budgets                  │  │ ┌──────────────────────┐ │ │
│  │                            │  │ │ ARCHITECTURE (2)     │ │ │
│  │                            │  │ │ ┌──────────────────┐ │ │ │
│  │                            │  │ │ │ Project Stack    │ │ │ │
│  │                            │  │ │ └──────────────────┘ │ │ │
│  │                            │  │ │ ┌──────────────────┐ │ │ │
│  │                            │  │ │ │ Code Structure   │ │ │ │
│  │                            │  │ │ └──────────────────┘ │ │ │
│  │                            │  │ └──────────────────────┘ │ │
│  │                            │  │ ┌──────────────────────┐ │ │
│  │                            │  │ │ IDEAS (0)            │ │ │
│  │                            │  │ │ ┌──────────────────┐ │ │ │
│  │                            │  │ │ │ (empty)          │ │ │ │
│  │                            │  │ │ └──────────────────┘ │ │ │
│  │                            │  │ └──────────────────────┘ │ │
│  │                            │  │ ┌──────────────────────┐ │ │
│  │                            │  │ │ PLANNING (0)         │ │ │
│  │                            │  │ │ ┌──────────────────┐ │ │ │
│  │                            │  │ │ │ (empty)          │ │ │ │
│  │                            │  │ │ └──────────────────┘ │ │ │
│  │                            │  │ └──────────────────────┘ │ │
│  │                            │  │ ┌──────────────────────┐ │ │
│  │                            │  │ │ IN PROGRESS (0)      │ │ │
│  │                            │  │ │ ┌──────────────────┐ │ │ │
│  │                            │  │ │ │ (empty)          │ │ │ │
│  │                            │  │ │ └──────────────────┘ │ │ │
│  │                            │  │ └──────────────────────┘ │ │
│  │                            │  │ ┌──────────────────────┐ │ │
│  │                            │  │ │ IN REVIEW (0)        │ │ │
│  │                            │  │ │ ┌──────────────────┐ │ │ │
│  │                            │  │ │ │ (empty)          │ │ │ │
│  │                            │  │ │ └──────────────────┘ │ │ │
│  │                            │  │ └──────────────────────┘ │ │
│  │                            │  │ ┌──────────────────────┐ │ │
│  │                            │  │ │ COMPLETED (0)        │ │ │
│  │                            │  │ │ ┌──────────────────┐ │ │ │
│  │                            │  │ │ │ (empty)          │ │ │ │
│  │                            │  │ │ └──────────────────┘ │ │ │
│  │                            │  │ └──────────────────────┘ │ │
│  │                            │  └──────────────────────────┘ │
│  └─────────────────────────────┴──────────────────────────────┘
└─────────────────────────────────────────────────────────────────┘
```

## 2. Component Specifications

### 2.1 Sidebar
- **Width:** 240px
- **Background:** #212529 (dark)
- **Color:** #fff (white text)
- **Sections:**
  - Projects section with project tabs
  - Global Config section with LLM and Budget management

### 2.2 Board Header
- **Title:** Large, bold project name
- **Project Selector:** Display current project
- **Refresh Button:** Blue (#0d6efd) with refresh icon

### 2.3 Kanban Columns
- **Layout:** Flexbox, horizontal scrolling
- **Columns (6 total):**
  1. ARCHITECTURE (purple border #6f42c1)
  2. IDEAS (cyan border #17a2b8)
  3. PLANNING (yellow border #ffc107)
  4. IN PROGRESS (blue border #0d6efd)
  5. IN REVIEW (orange border #fd7e14)
  6. COMPLETED (green border #198754)

### 2.4 Task Card
```
┌─────────────────────────────────┐
│ Task Title                      │
│ [tag] [tag]  owner  [actions]   │
│                                 │
│ ┌───────────────────────────┐   │
│ │ Action Buttons            │   │
│ │ [Edit] [Delete]           │   │
│ └───────────────────────────┘   │
└─────────────────────────────────┘
```

**Task Card Properties:**
- **Background:** #fff (white)
- **Border-left:** 4px colored by status
- **Padding:** 0.75rem
- **Border-radius:** 4px
- **Shadow:** 0 1px 2px rgba(0,0,0,0.1)
- **Cursor:** grab (grabbing when dragging)

### 2.5 Task Card States

#### 2.5.1 Normal State
- Standard styling with colored left border

#### 2.5.2 Rejected State
- Red left border (#dc3545)
- Red shadow
- Rejection badge showing count
- Clickable to view transition details

#### 2.5.3 Processing State
- Yellow animated border pulse
- Spinner indicator in title
- "Processing..." button text

### 2.6 Modal Components

#### 2.6.1 Add/Edit Task Modal
- **Width:** 500px (max 90vw)
- **Sections:**
  - Task Title (required)
  - Description (textarea)
  - Tags (comma-separated input)
  - Owner (text input)
  - Architecture-specific fields (conditional)
  - LLM Endpoint dropdown (conditional)
  - Budget dropdown (conditional)

#### 2.6.2 Architecture Content Fields
- Frontend
- Backend
- Database
- Style
- DAGs
- Config
- REPL
- Tests

#### 2.6.3 New Project Modal
- Simple form with project name input

#### 2.6.4 LLM Management Modal
- **Width:** 640px
- **Tabs:** Add New, Edit
- **Fields:**
  - Address
  - Port
  - Model Name
  - Parallel Sessions
  - Max Context
  - Notes

#### 2.6.5 Budget Management Modal
- **Width:** 600px
- **Fields:**
  - Budget Name

#### 2.6.6 Transition Failure Modal
- **Width:** 700px
- **Sections:**
  - Transition header with outcome
  - Vote cards (if applicable)
  - Token usage stats
  - Timestamp

#### 2.6.7 Task History Modal
- **Width:** 500px
- **Sections:**
  - Task details
  - Timeline of proof-of-work entries

## 3. Color Palette

| Element | Color | Hex |
|---------|-------|-----|
| Background | Light Gray | #f5f6f8 |
| Sidebar | Dark | #212529 |
| Sidebar Text | White | #fff |
| Sidebar Header | Muted | #6c757d |
| Column Background | Light Gray | #dee2e6 |
| Task Card | White | #fff |
| Primary Button | Blue | #0d6efd |
| Secondary Button | Gray | #6c757d |
| Success | Green | #198754 |
| Warning | Yellow | #ffc107 |
| Danger | Red | #dc3545 |
| Info | Cyan | #17a2b8 |
| Architecture | Purple | #6f42c1 |

## 4. Typography

| Element | Font Size | Weight |
|---------|-----------|--------|
| Board Title | 1.5rem | 600 |
| Column Title | 0.95rem | 600 |
| Task Title | 0.9rem | 500 |
| Tag | 0.65rem | 700 |
| Meta Text | 0.75rem | 400 |
| Modal Title | 1.2rem | 600 |
| Form Label | 0.85rem | 500 |
| Form Input | 0.9rem | 400 |

## 5. Interactions

### 5.1 Drag and Drop
- **Drag Start:** Card collapses to 1px height
- **Drag Over:** Ghost placeholder appears at insertion point
- **Drop:** Card moves to new position, API call to reorder
- **Invalid Drop:** Drop effect set to 'none'

### 5.2 Column Progression
- **Idea → Planning:** Requires description, LLM, budget
- **Planning → Development:** Requires description, LLM, budget
- **Development → Review:** Requires description, LLM, budget
- **Review → Completed:** Requires description, LLM, budget

### 5.3 WIP Limits
- Architecture: 10
- Idea: 15
- Planning: 10
- Development: 5
- Review: 5
- Completed: 15

### 5.4 Auto-Refresh
- Polls database every 5 seconds
- Updates UI when changes detected

## 6. Responsive Behavior

- **Mobile:** Sidebar becomes bottom navigation
- **Tablet:** Columns stack vertically on small screens
- **Desktop:** Full horizontal layout

## 7. Accessibility

- All interactive elements have keyboard focus states
- Drag and drop has keyboard alternatives
- Color contrast meets WCAG AA standards
- ARIA labels on dynamic content

## 8. Animations

- **Hover:** 0.2s transition on interactive elements
- **Drag:** 0.1s transform on card movement
- **Modal:** Fade in/out with backdrop
- **Error:** 0.25s fade in with slide down
- **Processing:** 1.5s infinite spin animation

## 9. Future Enhancements

- [ ] Dark mode toggle
- [ ] Custom column ordering
- [ ] Bulk operations
- [ ] Keyboard shortcuts
- [ ] Export/import board state
- [ ] Real-time collaboration indicators
