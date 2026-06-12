import pygame
import random
import math

# Инициализация Pygame
pygame.init()

# Размеры окна
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 600
screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
pygame.display.set_caption("Простой Minecraft на Python")

# Цвета
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
GREEN = (0, 128, 0)
BROWN = (139, 69, 19)
GRAY = (128, 128, 128)
BLUE = (0, 0, 255)
RED = (255, 0, 0)
YELLOW = (255, 255, 0)

# Параметры мира
WORLD_WIDTH = 20
WORLD_HEIGHT = 20
BLOCK_SIZE = 20

# Класс блока
class Block:
    def __init__(self, x, y, z, color):
        self.x = x
        self.y = y
        self.z = z
        self.color = color

# Генерация мира
world = []
for x in range(WORLD_WIDTH):
    for y in range(WORLD_HEIGHT):
        if random.random() > 0.3:
            world.append(Block(x, y, 0, GREEN))  # Трава
        else:
            world.append(Block(x, y, 0, BROWN))  # Земля

# Камера
camera_x = 0
camera_y = 0

# Блоки для постройки
block_colors = [GREEN, BROWN, GRAY, BLUE, RED, YELLOW]
selected_block_color = block_colors[0]

# Функция для преобразования 3D-координат в 2D-экранные
def project_3d_to_2d(x, y, z):
    # Изометрическая проекция
    screen_x = (x - y) * BLOCK_SIZE + SCREEN_WIDTH // 2 - camera_x
    screen_y = (x + y) * BLOCK_SIZE // 2 - z * BLOCK_SIZE + SCREEN_HEIGHT // 2 - camera_y
    return (int(screen_x), int(screen_y))

# Функция для рисования блока
def draw_block(block):
    x, y = project_3d_to_2d(block.x, block.y, block.z)
    
    # Верхняя грань
    points_top = [
        (x, y),
        (x + BLOCK_SIZE, y),
        (x + BLOCK_SIZE // 2, y - BLOCK_SIZE // 2)
    ]
    pygame.draw.polygon(screen, block.color, points_top)
    
    # Правая грань
    points_right = [
        (x + BLOCK_SIZE, y),
        (x + BLOCK_SIZE, y + BLOCK_SIZE // 2),
        (x + BLOCK_SIZE // 2, y + BLOCK_SIZE)
    ]
    darker_color = tuple(int(c * 0.8) for c in block.color)
    pygame.draw.polygon(screen, darker_color, points_right)
    
    # Левая грань
    points_left = [
        (x, y),
        (x + BLOCK_SIZE // 2, y + BLOCK_SIZE),
        (x, y + BLOCK_SIZE // 2)
    ]
    darker_color = tuple(int(c * 0.6) for c in block.color)
    pygame.draw.polygon(screen, darker_color, points_left)

# Основной игровой цикл
clock = pygame.time.Clock()
running = True

while running:
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        
        # Движение камеры
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_LEFT:
                camera_x -= 10
            if event.key == pygame.K_RIGHT:
                camera_x += 10
            if event.key == pygame.K_UP:
                camera_y -= 10
            if event.key == pygame.K_DOWN:
                camera_y += 10
        
        # Установка блока
        if event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 1:  # Левая кнопка мыши - разрушение
                mouse_x, mouse_y = pygame.mouse.get_pos()
                # Упрощенное определение блока под курсором
                for block in world:
                    bx, by = project_3d_to_2d(block.x, block.y, block.z)
                    if abs(mouse_x - bx) < BLOCK_SIZE and abs(mouse_y - by) < BLOCK_SIZE:
                        world.remove(block)
                        break
            if event.button == 3:  # Правая кнопка мыши - постройка
                mouse_x, mouse_y = pygame.mouse.get_pos()
                # Упрощенное определение позиции для нового блока
                new_x = int((mouse_x - SCREEN_WIDTH // 2 + camera_x) / BLOCK_SIZE + (mouse_y - SCREEN_HEIGHT // 2 + camera_y) / BLOCK_SIZE)
                new_y = int((mouse_y - SCREEN_HEIGHT // 2 + camera_y) / BLOCK_SIZE - (mouse_x - SCREEN_WIDTH // 2 + camera_x) / BLOCK_SIZE)
                
                # Проверка, нет ли уже блока в этой позиции
                block_exists = False
                for block in world:
                    if block.x == new_x and block.y == new_y and block.z == 0:
                        block_exists = True
                        break
                
                if not block_exists:
                    world.append(Block(new_x, new_y, 0, selected_block_color))
        
        # Смена блока
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_1:
                selected_block_color = block_colors[0]
            if event.key == pygame.K_2:
                selected_block_color = block_colors[1]
            if event.key == pygame.K_3:
                selected_block_color = block_colors[2]
            if event.key == pygame.K_4:
                selected_block_color = block_colors[3]
            if event.key == pygame.K_5:
                selected_block_color = block_colors[4]
            if event.key == pygame.K_6:
                selected_block_color = block_colors[5]

    # Очистка экрана
    screen.fill(BLACK)
    
    # Рисование всех блоков
    for block in world:
        draw_block(block)
    
    # Отображение выбранного блока
    font = pygame.font.Font(None, 24)
    text = font.render(f"Выбран блок: {selected_block_color}", True, WHITE)
    screen.blit(text, (10, 10))
    
    # Обновление экрана
    pygame.display.flip()
    
    # Контроль частоты кадров
    clock.tick(60)

pygame.quit()